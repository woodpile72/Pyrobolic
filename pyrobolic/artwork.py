# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Artwork requests and verification inside owned download staging."""
import json
from pathlib import Path
import subprocess

MODES = ["off", "save", "embed", "both"]
LABELS = ["Off", "Save Image", "Embed Only", "Embed and Save Image"]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".tiff"}


def artwork_mode(options):
    return "off" if getattr(options, "auth_ref", None) else options.artwork_mode


def artwork_summary(options):
    if getattr(options, "auth_ref", None):
        return "Disabled for Authentication"
    summary = LABELS[MODES.index(options.artwork_mode)]
    if options.artwork_mode != "off" and options.square_audio_artwork and options.mode == "audio":
        summary += " · Square JPEG"
    return summary


def artwork_arguments(job):
    mode = artwork_mode(job)
    if mode == "off":
        return []
    if job.output_stem is None:
        raise ValueError("Artwork requires a new named download with owned staging.")
    args = ["--write-thumbnail", "--print", "before_dl:PYRO_ART_AVAILABLE:%(thumbnail|[])j",
            "--print", "after_move:PYRO_ARTFILES:%(thumbnails.:.filepath|[])j"]
    if mode in {"embed", "both"}:
        args.append("--embed-thumbnail")
    if job.square_audio_artwork and job.mode == "audio":
        # Two formats ensure even a JPEG source passes through the crop converter.
        args += ["--use-postprocessor", "FFmpegThumbnailsConvertor:format=png;when=before_dl",
                 "--convert-thumbnails", "jpg", "--postprocessor-args",
                 'ThumbnailsConvertor+ffmpeg_o:-vf "crop=min(iw\\,ih):min(iw\\,ih)" -q:v 2']
    return args


def validate_artwork(options):
    if artwork_mode(options) not in {"embed", "both"}:
        return
    if ((options.mode == "video" and options.container in {"avi", "webm"}) or
            (options.mode == "audio" and options.audio_format == "wav")):
        raise ValueError("This output format cannot embed artwork. Choose Save Image or a compatible format.")


def discard_failed_images(stage):
    """Failed engine conversion can leave an unusable thumbnail cached for Retry."""
    if stage is None:
        return
    for path in stage.glob("media.*"):
        if path.suffix.lower() in IMAGE_SUFFIXES and not path.is_symlink() and path.is_file():
            path.unlink()


def finish_artwork(job, stage, files, available, run):
    """Validate image outputs and embedding before exposing any files to publication."""
    from .core import verify_outputs
    mode = artwork_mode(job)
    if mode == "off":
        return []
    if available is None:
        raise RuntimeError("The engine did not report artwork availability.")
    if not files:
        if available:
            raise RuntimeError("Source artwork was available but no image was saved. Retry the download.")
        job.warnings += ["No source artwork available; no artwork was saved or embedded."]
        return []
    paths = list(dict.fromkeys(files))
    for name in paths:
        path = Path(name)
        if (stage is None or path.parent != stage or not path.name.startswith("media.") or path.is_symlink() or
                path.suffix.lower() not in IMAGE_SUFFIXES):
            raise RuntimeError("An artwork output is outside this download's staging directory.")
        if path.is_file() and path.stat().st_size == 0:
            path.unlink()
            raise RuntimeError("The source artwork is empty. Retry to fetch it again.")
    verify_outputs(paths)
    for path in paths:
        try:
            image = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path]))
            if not any(stream.get("codec_type") == "video" and stream.get("width", 0) > 0 and
                       stream.get("height", 0) > 0 for stream in image.get("streams", [])):
                raise ValueError("No image stream")
            if job.square_audio_artwork and job.mode == "audio" and any(
                    stream.get("codec_type") == "video" and stream.get("width") != stream.get("height")
                    for stream in image.get("streams", [])):
                raise ValueError("Artwork was not cropped square")
            run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", path, "-frames:v", "1", "-f", "null", "-"])
        except (ValueError, subprocess.CalledProcessError) as error:
            # Remove only the invalid owned image so Retry can fetch a fresh copy.
            Path(path).unlink()
            raise RuntimeError("The source artwork is not a readable image. Retry to fetch it again.") from error
    if mode in {"embed", "both"}:
        for media in job.files:
            result = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", media]))
            if not any(stream.get("disposition", {}).get("attached_pic") or
                       (stream.get("codec_type") == "attachment" and
                        stream.get("tags", {}).get("mimetype", "").startswith("image/"))
                       for stream in result.get("streams", [])):
                raise RuntimeError("Requested artwork was not embedded in the output. Choose Save Image or a compatible format.")
    if mode == "embed":
        for path in paths:
            Path(path).unlink()
        return []
    return paths
