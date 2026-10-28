# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Output encoding policy, separate from source selection and lossless remuxing."""
import shlex
import json
import math
from pathlib import Path

ENCODERS = ["default", "h264", "hevc", "vp9", "av1", "mpeg4"]
ENCODER_LABELS = ["Container Default", "H.264", "H.265 (HEVC)", "VP9", "AV1", "MPEG-4 Part 2"]
QUALITIES = ["default", "high", "balanced", "small"]
QUALITY_LABELS = ["Encoder Default", "Higher Quality", "Balanced", "Smaller File"]
COMPATIBLE = {"mp4": {"h264", "hevc", "av1"}, "mkv": set(ENCODERS[1:]),
              "mov": {"h264", "hevc"}, "webm": {"vp9", "av1"}, "avi": {"mpeg4"}}
DEFAULTS = {"mp4": "h264", "mkv": "h264", "mov": "h264", "webm": "vp9", "avi": "mpeg4"}
IMPLEMENTATIONS = {"h264": "libx264", "hevc": "libx265", "vp9": "libvpx-vp9", "av1": "libaom-av1", "mpeg4": "mpeg4"}
QUALITY_VALUES = {"h264": (18, 23, 28), "hevc": (20, 26, 32), "vp9": (24, 32, 40),
                  "av1": (24, 32, 40), "mpeg4": (2, 5, 8)}


def validate_encoding(options):
    if options.video_encoder not in ENCODERS or options.video_encoding_quality not in QUALITIES:
        raise ValueError("Invalid video encoding preference")
    if options.mode != "video" or options.video_processing != "convert":
        return
    if options.container not in COMPATIBLE:
        raise ValueError("Choose a target container before allowing video conversion")
    if options.video_encoder != "default" and options.video_encoder not in COMPATIBLE.get(options.container, set()):
        raise ValueError("The selected video encoder does not support this output container. Choose another encoder or container.")


def encoder_options(options, force=False):
    custom = force or options.video_encoder != "default" or options.video_encoding_quality != "default"
    if not custom and not options.video_encoding_threads:
        return []
    encoder = DEFAULTS[options.container] if options.video_encoder == "default" else options.video_encoder
    args = ["-c:v", IMPLEMENTATIONS[encoder]] if custom else []
    if custom and encoder == "av1":
        args += ["-cpu-used", "4"]
    if options.video_encoding_quality != "default":
        quality = QUALITY_VALUES[encoder][QUALITIES.index(options.video_encoding_quality) - 1]
        args += ["-q:v" if encoder == "mpeg4" else "-crf", str(quality)]
        if encoder in {"vp9", "av1"}:
            args += ["-b:v", "0"]
    if options.video_encoding_threads:
        threads = options.video_encoding_threads
        args += ["-threads", str(threads)]
        if custom and encoder == "hevc":
            args += ["-x265-params", f"pools={threads}:frame-threads={min(16, threads)}"]
    return args


def reencoding_enabled(options):
    return options.reencode_video and options.mode == "video" and options.video_processing == "convert"


def encoding_arguments(options):
    validate_encoding(options)
    if options.mode != "video" or options.video_processing != "convert":
        return []
    args = []
    if reencoding_enabled(options):
        if getattr(options, "output_stem", None) is None:
            raise ValueError("Always re-encoding requires a named download with owned staging.")
        args += ["--print", "before_dl:PYRO_INPUT_EXT:%(ext)j"]
    settings = encoder_options(options, force=reencoding_enabled(options))
    # Restrict options to conversion output, never merging, subtitles, remux or audio extraction.
    if settings:
        args += ["--postprocessor-args", "VideoConvertor+ffmpeg_o:" + shlex.join(settings)]
    return args


def finish_encoding(options, stage, input_ext, files, run, preserve):
    """Fill the engine's same-container conversion gap without encoding its conversions twice."""
    from .core import verify_outputs
    if not reencoding_enabled(options):
        return
    if stage is None or not isinstance(input_ext, str) or not input_ext:
        raise RuntimeError("The engine did not report the input container; re-encoding cannot be verified.")
    encoder = DEFAULTS[options.container] if options.video_encoder == "default" else options.video_encoder
    for name in dict.fromkeys(files):
        media = Path(name)
        if (media.parent != stage or not media.name.startswith("media.") or media.is_symlink()
                or media.suffix != "." + options.container):
            raise RuntimeError("Re-encoding output is outside staging or has the wrong container.")
        verify_outputs([str(media)])
        if input_ext == options.container:
            original = stage / ("media.encoding-source." + media.name[len("media."):])
            temporary = stage / ("media.encoding-output." + media.name[len("media."):])
            if original.is_symlink() or temporary.is_symlink():
                raise RuntimeError("Re-encoding staging contains a symbolic link.")
            # Keep an immutable input across failure, cancellation and publication retries.
            if not original.exists():
                preserve(media, original)
            verify_outputs([str(original)])
            streams = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(original)]))["streams"]
            videos = [index for index, stream in enumerate(streams)
                      if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
            if not videos:
                raise RuntimeError("No video stream was found to re-encode.")
            args = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(original),
                    "-map", "0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
            for index in videos:
                settings = encoder_options(options, force=True)
                for position in range(0, len(settings), 2):
                    key, value = settings[position:position + 2]
                    # Numeric output indices preserve attached pictures and non-video streams.
                    args += [f"{key.removesuffix(':v')}:{index}", value]
            run(args + [str(temporary)], timeout=86400)
            candidate = temporary
        else:
            # yt-dlp already converted a different container using our encoder options.
            candidate = media
        verify_outputs([str(candidate)])
        result = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(candidate)]))
        videos = [stream for stream in result.get("streams", [])
                  if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
        if not videos or any(stream.get("codec_name") != encoder for stream in videos):
            raise RuntimeError("The output does not contain the requested video codec.")
        duration = float(result.get("format", {}).get("duration", 0))
        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError("The re-encoded output has no playable duration.")
        if candidate != media and [s.get("codec_type") for s in streams] != [s.get("codec_type") for s in result.get("streams", [])]:
            raise RuntimeError("Re-encoding did not preserve all media streams.")
        if candidate != media:
            candidate.replace(media)


def encoding_summary(options):
    summary = ENCODER_LABELS[ENCODERS.index(options.video_encoder)] + " · " + QUALITY_LABELS[QUALITIES.index(options.video_encoding_quality)]
    return summary + (f" · {options.video_encoding_threads} threads" if options.video_encoding_threads else "") + (" · Always Re-encode" if options.reencode_video else "")


def encoding_detail(options):
    if options.video_encoder == "default" and options.video_encoding_quality == "default" and not options.reencode_video:
        return "FFmpeg's default encoding settings"
    encoder = DEFAULTS[options.container] if options.video_encoder == "default" else options.video_encoder
    name = ENCODER_LABELS[ENCODERS.index(encoder)]
    if options.video_encoding_quality == "default":
        return name + " · Encoder Default"
    value = QUALITY_VALUES[encoder][QUALITIES.index(options.video_encoding_quality) - 1]
    return f"{name} · {'Quantizer' if encoder == 'mpeg4' else 'CRF'} {value}"
