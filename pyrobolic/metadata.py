# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Independent source chapter and Matroska source-data attachment policies."""

POLICIES = ["metadata", "embed", "omit"]
POLICY_LABELS = ["Follow Metadata Setting", "Add When Available", "Do Not Add"]


def effective_policy(options, field):
    # Authenticated jobs must not export source-bearing metadata, even on retry.
    return "omit" if getattr(options, "auth_ref", None) else getattr(options, field)


def verify_chapters(options):
    policy = effective_policy(options, "chapter_policy")
    return policy == "embed" or (policy == "metadata" and options.metadata and
                                  (getattr(options, "clip_start", 0) or getattr(options, "clip_end", None) is not None))


def splitting_enabled(options):
    return options.split_chapters and not getattr(options, "auth_ref", None)


def description_enabled(options):
    return options.save_description and not getattr(options, "auth_ref", None)


def validate_chapter_output(options):
    if splitting_enabled(options) and (getattr(options, "clip_start", 0) or getattr(options, "clip_end", None) is not None):
        raise ValueError("Chapter splitting requires the entire media. Clear the time range or turn off chapter splitting.")


def metadata_arguments(options):
    args = ["--embed-metadata"] if options.metadata else []
    if description_enabled(options):
        if getattr(options, "output_stem", None) is None:
            raise ValueError("Description files require a new named download with owned staging.")
        args += ["--write-description",
                 "--print", "before_dl:PYRO_DESCRIPTION:%(description&1|0)s"]
    for field, flag in (("chapter_policy", "chapters"), ("source_info_policy", "info-json")):
        policy = effective_policy(options, field)
        if policy != "metadata":
            args.append(f"--{'no-' if policy == 'omit' else ''}embed-{flag}")
    if verify_chapters(options) or splitting_enabled(options):
        args += ["--print", "before_dl:PYRO_CHAPTERS:%(chapters|[])j"]
    if splitting_enabled(options):
        if getattr(options, "output_stem", None) is None:
            raise ValueError("Chapter splitting requires a new named download with owned staging.")
        # Leave room for the app's 200-byte output stem and collision suffixes.
        chapter_name = "chapter:media.chapter-%(section_number)03d"
        if options.chapter_titles:
            chapter_name += "-%(section_title|Chapter).32B"
        args += ["--split-chapters", "--output", chapter_name + ".%(ext)s",
                 "--print", "after_move:PYRO_CHAPTERFILES:%(chapters.:.filepath|[])j"]
        if options.precise_chapters and options.mode == "video":
            args.append("--force-keyframes-at-cuts")
    return args


def expected_chapters(options, chapters):
    """Only require chapters overlapping the requested time range."""
    start, end = options.clip_start, options.clip_end
    return any(isinstance(chapter, dict) and
               type(chapter.get("start_time")) in (int, float) and
               type(chapter.get("end_time")) in (int, float) and
               chapter["end_time"] > start and
               (end is None or chapter["start_time"] < end) for chapter in chapters)


def metadata_summary(options):
    if getattr(options, "auth_ref", None):
        return "Adding Source Chapters and Source Data: Disabled for Authentication"
    summary = "Chapters: " + POLICY_LABELS[POLICIES.index(options.chapter_policy)] + " · Source Data: " + POLICY_LABELS[POLICIES.index(options.source_info_policy)]
    if options.split_chapters:
        summary += " · Separate Chapter Files" + (" (Precise Video Cuts)" if options.precise_chapters else "")
        if options.chapter_titles:
            summary += " · Chapter Titles in Filenames"
    if options.save_description:
        summary += " · Description File"
    return summary


def description_outputs(job, stage, available):
    from .core import verify_outputs
    if not description_enabled(job):
        return []
    if available is None:
        raise RuntimeError("The engine did not report description availability.")
    if not available:
        job.warnings += ["No source description available; no description file was saved."]
        return []
    if stage is None:
        raise RuntimeError("Description files require owned staging.")
    path = stage / "media.description"
    if path.is_symlink():
        raise RuntimeError("The description file is not an owned regular file.")
    if path.is_file() and path.stat().st_size == 0:
        # The engine skips existing sidecars; discard only our empty staging file for Retry.
        path.unlink()
        raise RuntimeError("The description file is empty. Retry to fetch it again.")
    verify_outputs([str(path)])
    return [str(path)]


def clipped_metadata(options, chapters):
    """Rebase source chapters explicitly; the engine may retain full-video timestamps."""
    lines = [";FFMETADATA1"]
    for chapter in chapters:
        if not expected_chapters(options, [chapter]):
            continue
        start = max(0, chapter["start_time"] - options.clip_start)
        end = chapter["end_time"] - options.clip_start
        if options.clip_end is not None:
            end = min(end, options.clip_end - options.clip_start)
        title = str(chapter.get("title") or "").replace("\x00", "")
        for character in ("\\", "=", ";", "#", "\n"):
            title = title.replace(character, "\\" + character)
        lines.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(start * 1000)}",
                      f"END={round(end * 1000)}", "title=" + title])
    return "\n".join(lines) + "\n"


def chapter_outputs(job, stage, chapters, files, run):
    import json
    from pathlib import Path
    from .core import verify_outputs
    if not splitting_enabled(job):
        return []
    if chapters is None:
        raise RuntimeError("The engine did not report source chapters for splitting.")
    if not chapters:
        job.warnings += ["No source chapters available; only the full media file was saved."]
        return []
    paths = list(dict.fromkeys(files))
    if len(paths) != len(chapters):
        raise RuntimeError("The engine did not produce all requested chapter files.")
    for name in paths:
        path = Path(name)
        if stage is None or path.parent != stage or not path.name.startswith("media.chapter-") or path.is_symlink():
            raise RuntimeError("A chapter file is outside this download's staging directory.")
    verify_outputs(paths)
    for path in paths:
        result = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]))
        if (not any(stream.get("codec_type") in {"video", "audio"} and
                    not stream.get("disposition", {}).get("attached_pic") for stream in result.get("streams", [])) or
                float(result.get("format", {}).get("duration", 0)) <= 0):
            raise RuntimeError("A chapter file contains no playable media. Try Precise Video Cuts.")
    return paths
