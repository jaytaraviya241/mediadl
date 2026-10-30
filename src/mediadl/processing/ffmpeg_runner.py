"""Safe FFmpeg/FFprobe invocation (spec section 12.10a).

Rules that matter here:
  * argument lists only, never a shell;
  * ``-nostdin`` and a protocol allowlist, so a crafted container or playlist
    cannot make FFmpeg fetch anything from the network during local work;
  * wall-clock timeout and POSIX rlimits, with the whole process group killed;
  * cancellation stops FFmpeg instead of orphaning it.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mediadl.errors import ProcessingError, ProcessingTimeoutError, ToolMissingError
from mediadl.net.redact import redact_argv
from mediadl.obs.logging import get_logger

log = get_logger(__name__)

#: Local processing never needs the network: inputs are already on disk.
LOCAL_PROTOCOLS = 'file,pipe'
#: Used only by the streaming delegate engine, after the manifest is vetted.
REMOTE_PROTOCOLS = 'https,tls,tcp,crypto,file,pipe'


@dataclass(frozen=True, slots=True)
class FfmpegTools:
    ffmpeg: str
    ffprobe: str
    version: str = ''

    @classmethod
    def discover(cls, configured_path: str | None = None) -> FfmpegTools:
        directory = configured_path or None

        def find(name: str) -> str:
            found = shutil.which(name, path=directory) if directory else shutil.which(name)
            if not found:
                raise ToolMissingError(
                    f'{name} was not found; install FFmpeg or set [ffmpeg] path',
                    tool=name,
                )
            return found

        return cls(find('ffmpeg'), find('ffprobe'))

    @classmethod
    def try_discover(cls, configured_path: str | None = None) -> FfmpegTools | None:
        try:
            return cls.discover(configured_path)
        except ToolMissingError:
            return None


def _limit_resources(max_cpu_s: int, max_file_bytes: int) -> Callable[[], None]:
    """POSIX-only preexec hook applying CPU and output-size rlimits."""

    def apply() -> None:  # pragma: no cover - runs in the forked child
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (max_cpu_s, max_cpu_s))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))

    return apply


def _signal_tree(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        if os.name == 'posix':
            os.killpg(proc.pid, sig)
        elif sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        pass


async def _stop(proc: asyncio.subprocess.Process) -> None:
    _signal_tree(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError:
        _signal_tree(proc, signal.SIGKILL)
        await proc.wait()


async def run(
    argv: list[str],
    *,
    timeout_s: float,
    max_cpu_s: int = 3600,
    max_file_bytes: int = 8 * 1024**3,
    on_progress: Callable[[str, str], None] | None = None,
) -> tuple[bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_limit_resources(max_cpu_s, max_file_bytes) if os.name == 'posix' else None,
        start_new_session=True,  # own process group, so the tree can be killed
    )

    async def pump_stdout() -> bytes:
        chunks: list[bytes] = []
        assert proc.stdout is not None
        async for line in proc.stdout:
            chunks.append(line)
            if on_progress and b'=' in line:
                key, _, value = line.decode('utf-8', 'replace').strip().partition('=')
                on_progress(key, value)
        return b''.join(chunks)

    try:
        assert proc.stderr is not None
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(pump_stdout(), proc.stderr.read()), timeout=timeout_s
        )
        await proc.wait()
    except TimeoutError:
        await _stop(proc)
        raise ProcessingTimeoutError(
            f'{Path(argv[0]).name} exceeded {timeout_s}s',
            tool=Path(argv[0]).name, timeout_s=timeout_s,
        ) from None
    except asyncio.CancelledError:
        await _stop(proc)
        raise

    if proc.returncode != 0:
        tail = stderr.decode('utf-8', 'replace').strip().splitlines()[-5:]
        raise ProcessingError(
            f'{Path(argv[0]).name} exited {proc.returncode}',
            tool=Path(argv[0]).name,
            returncode=proc.returncode,
            stderr_tail=' | '.join(tail),
            argv=list(redact_argv(argv)),
        )
    return stdout, stderr


async def probe_json(tools: FfmpegTools, path: Path, *, timeout_s: float = 30) -> dict:
    argv = [
        tools.ffprobe, '-v', 'error', '-protocol_whitelist', 'file',
        '-show_entries',
        'format=format_name,duration,size,bit_rate:'
        'stream=index,codec_type,codec_name,profile,width,height,pix_fmt,'
        'avg_frame_rate,r_frame_rate,bit_rate,sample_rate,channels',
        '-of', 'json', str(path),
    ]
    stdout, _ = await run(argv, timeout_s=timeout_s)
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise ProcessingError('ffprobe returned invalid JSON') from exc


async def stream_md5(tools: FfmpegTools, path: Path, selector: str = '0:v:0') -> str:
    """Container-independent hash of one stream's packets.

    Proves that a "stream copy" really did not touch the compressed data.
    """
    stdout, _ = await run(
        [tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
         '-protocol_whitelist', LOCAL_PROTOCOLS, '-i', str(path),
         '-map', selector, '-c', 'copy', '-f', 'md5', '-'],
        timeout_s=120,
    )
    return stdout.decode().strip().removeprefix('MD5=')


async def frame_md5(tools: FfmpegTools, path: Path, selector: str = '0:v:0') -> str:
    """Hash of the *decoded* frames, ignoring timestamps.

    Packet-level ``stream_md5`` is the stronger check, but it cannot be used
    across a container change: H.264 in MPEG-TS is Annex-B and in MP4 is
    AVCC, so a lossless remux necessarily repackages the NAL units. Decoded
    frames are identical either way, which is what "no re-encode" means.
    """
    import hashlib

    stdout, _ = await run(
        [tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
         '-protocol_whitelist', LOCAL_PROTOCOLS, '-i', str(path),
         '-map', selector, '-f', 'framemd5', '-'],
        timeout_s=300,
    )
    hashes = [
        line.rsplit(',', 1)[-1].strip()
        for line in stdout.decode('utf-8', 'replace').splitlines()
        if line and not line.startswith('#')
    ]
    return hashlib.sha256('\n'.join(hashes).encode()).hexdigest()


async def build_config(tools: FfmpegTools) -> str:
    stdout, stderr = await run([tools.ffmpeg, '-hide_banner', '-buildconf'], timeout_s=30)
    return (stdout + stderr).decode('utf-8', 'replace')


async def license_text(tools: FfmpegTools) -> str:
    stdout, stderr = await run([tools.ffmpeg, '-hide_banner', '-L'], timeout_s=30)
    return (stdout + stderr).decode('utf-8', 'replace')
