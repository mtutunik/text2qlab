import argparse
import subprocess
import sys
import os
import tempfile
import textwrap
import re


# -----------------------------
# CONFIG
# -----------------------------
MAX_CHARS_PER_CUE = 80  # approximate max characters per cue
LAYER = 0               # layer for all cues
OPACITY = 1.0           # cue opacity

MAX_SENTENCES_PER_CHUNK=2
MAX_LINES_PER_CHUNK=4
MAX_CHARS_PER_LINE=37

# Font settings for text cues (set FONT_FAMILY to None to keep QLab's default)
FONT_FAMILY = "Tahoma"   # font family name as shown in Font Book
FONT_STYLE = "Bold"           # e.g. "Regular", "Bold", "Italic"
FONT_SIZE = 72                   # points

# -----------------------------
# FUNCTION: Split text into chunks
# -----------------------------
def split_text(text, max_len):
    """
    Splits text into chunks of max_len characters, without breaking words.
    """
    words = text.split()
    chunks = []
    current = ""
    for word in words:
        if len(current) + len(word) + (1 if current else 0) <= max_len:
            current += (" " if current else "") + word
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks

# -----------------------------
# FUNCTION: Create a Text cue in QLab safely
# -----------------------------
def create_text_cue(chunk, layer=LAYER, opacity=OPACITY, speaker=None):
    """
    Writes chunk to a temp file and uses AppleScript to create a Text cue.
    This avoids any quoting issues.
    """
    # Write chunk to temp text file
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
        f.write(chunk)
        text_path = f.name

    # Build AppleScript

    speaker_note = f'"{speaker}"' if speaker else '""'

    if FONT_FAMILY:
        font_line = (
            f'set text format of newCue to {{fontFamily:"{FONT_FAMILY}", '
            f'fontStyle:"{FONT_STYLE}", fontSize:{FONT_SIZE}}}'
        )
    else:
        font_line = ""

    applescript = f'''
set chunkText to read POSIX file "{text_path}" as «class utf8»
set speaker_note to {speaker_note}
# make new text cue with properties {{text:chunkText, layer:{layer}, opacity:{opacity}}}

tell application id "com.figure53.QLab.5" to tell front workspace
    make type "Text"
    set selectedCues to selected as list
    set newCue to last item of (selected as list)
    set thecuenumber to q number of newCue
    set the text of newCue to chunkText
    {font_line}
    set notes of newCue to speaker_note
    set newCueID to uniqueID of newCue
end tell
return newCueID
'''

    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as f:
        f.write(applescript)
        script_path = f.name

    # Run the AppleScript (stdout is the new cue's uniqueID)
    result = subprocess.run(
        ["osascript", script_path], capture_output=True, text=True, encoding="utf-8"
    )
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    # Clean up temp files
    os.remove(text_path)
    os.remove(script_path)
    return result.stdout.strip() or None


def create_fadegroups():
     subprocess.run(["osascript", "fadegroup.applescript"])


RENAME_GROUPS_APPLESCRIPT = '''
on run argv
    tell application id "com.figure53.QLab.5" to tell front workspace
        repeat with i from 1 to (count of argv) by 2
            set textCue to cue id (item i of argv)
            set groupCue to parent of textCue
            if q type of groupCue is "Group" then
                set q name of groupCue to item (i + 1) of argv
            end if
        end repeat
    end tell
end run
'''


def rename_groups(named_cues):
    """
    named_cues: list of (text_cue_unique_id, group_name).
    Renames the group cue that contains each Text cue (run after fade groups exist).
    """
    args = []
    for cue_id, name in named_cues:
        args += [cue_id, name]
    subprocess.run(
        ["osascript", "-", *args], input=RENAME_GROUPS_APPLESCRIPT, text=True, encoding="utf-8"
    )


def read_chunks(file_path):
    """
    Generator that yields one chunk at a time from a file where
    chunks are separated by a line with '-------'.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        chunk_lines = []
        for line in f:
            line = line.rstrip("\n")  # remove newline
            if line == "-------":
                if chunk_lines:
                    yield "\n".join(chunk_lines)
                    chunk_lines = []
            else:
                chunk_lines.append(line)
        # yield last chunk if any
        if chunk_lines:
            yield "\n".join(chunk_lines)




# Marker line that names the group of the next cue, e.g. "[GROUP: Wedding photo]"
GROUP_MARKER_PATTERN = re.compile(r'^\[GROUP:\s*(.*?)\s*\]$')


def generate_chunks_from_file(
    file_path,
    max_sentences_per_chunk=MAX_SENTENCES_PER_CHUNK,
    max_lines_per_chunk=MAX_LINES_PER_CHUNK,
    max_chars_per_line=MAX_CHARS_PER_LINE
):
    """
    Streaming generator of (chunk, speaker, group_name) tuples that:
    - Detects speaker lines (e.g., STUDENT:, NARRATOR 2:)
    - Excludes speaker names from output
    - Starts a new chunk when speaker changes
    - Detects "[GROUP: name]" marker lines: starts a new chunk there and
      gives the first chunk after the marker that group name
      (group_name is None for all other chunks)
    - Respects sentence/line/char limits
    """

    speaker_pattern = re.compile(r'^[A-Z0-9 ]+:\s*')
    sentence_split_pattern = re.compile(r'(?<=[.!?])(?<!\.\.)\s+')

    current_sentences = []
    current_speaker = None
    pending_group = None

    def flush():
        nonlocal pending_group
        chunks = []
        for chunk in build_chunks_from_sentences(
            current_sentences,
            max_sentences_per_chunk,
            max_lines_per_chunk,
            max_chars_per_line
        ):
            chunks.append((chunk, current_speaker, pending_group))
            pending_group = None
        current_sentences.clear()
        return chunks

    with open(file_path, "r", encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.strip()

            if not line:
                continue

            # -----------------------------
            # Detect group marker
            # -----------------------------
            group_match = GROUP_MARKER_PATTERN.match(line)
            if group_match:
                yield from flush()
                pending_group = group_match.group(1) or None
                continue

            # -----------------------------
            # Detect speaker
            # -----------------------------
            speaker_match = speaker_pattern.match(line)
            if speaker_match:
                # New speaker → flush current chunk first
                yield from flush()

                current_speaker = re.sub(r'[:：\s]+$', '', speaker_match.group())
                line = line[speaker_match.end():].strip()

                if not line:
                    continue  # speaker line only, no text

            # -----------------------------
            # Split into sentences
            # -----------------------------
            sentences = sentence_split_pattern.split(line)

            for sentence in sentences:
                sentence = sentence.strip()
                if sentence:
                    current_sentences.append(sentence)

        # Flush remaining text at end of file
        yield from flush()


def build_chunks_from_sentences(
    sentences,
    max_sentences_per_chunk,
    max_lines_per_chunk,
    max_chars_per_line
):
    """
    Builds properly formatted chunks from sentence list.
    """

    i = 0
    while i < len(sentences):
        chunk_sentences = sentences[i:i + max_sentences_per_chunk]
        i += max_sentences_per_chunk

        chunk_text = " ".join(chunk_sentences)

        lines = textwrap.wrap(
            chunk_text,
            width=max_chars_per_line,
            break_long_words=False,
            break_on_hyphens=False
        )

        for j in range(0, len(lines), max_lines_per_chunk):
            yield "\n".join(lines[j:j + max_lines_per_chunk])



# -----------------------------
# MAIN
# -----------------------------
def ask_int(prompt, default):
    """
    Asks the user for a positive integer. Pressing Enter keeps the default.
    """
    while True:
        try:
            answer = input(f"{prompt} [{default}]: ").strip()
        except EOFError:  # no interactive input available
            print()
            return default
        if not answer:
            return default
        try:
            value = int(answer)
        except ValueError:
            print("  Please enter a whole number.")
            continue
        if value < 1:
            print("  Please enter a number of at least 1.")
            continue
        return value


def main():
    parser = argparse.ArgumentParser(
        description="Create QLab Text cues from a 'SPEAKER: lines' script file."
    )
    parser.add_argument("text_file", help="Script file to read")
    parser.add_argument(
        "--max-sentences", type=int,
        help=f"Max sentences per cue (default: {MAX_SENTENCES_PER_CHUNK})",
    )
    parser.add_argument(
        "--max-lines", type=int,
        help=f"Max lines per cue (default: {MAX_LINES_PER_CHUNK})",
    )
    parser.add_argument(
        "--max-chars", type=int,
        help=f"Max characters per line (default: {MAX_CHARS_PER_LINE})",
    )
    parser.add_argument(
        "--default", action="store_true",
        help="Don't ask questions; use the hardcoded values for any limit "
             "not given on the command line",
    )
    args = parser.parse_args()

    # Limits given on the command line are used as-is. The rest are asked
    # interactively, or taken from the hardcoded values with --default.
    limits = (
        ("max_chars", "Max characters per line", MAX_CHARS_PER_LINE),
        ("max_lines", "Max lines per cue", MAX_LINES_PER_CHUNK),
        ("max_sentences", "Max sentences per cue", MAX_SENTENCES_PER_CHUNK),
    )
    for name, prompt, default in limits:
        if getattr(args, name) is None:
            value = default if args.default else ask_int(prompt, default)
            setattr(args, name, value)

    for name in ("max_sentences", "max_lines", "max_chars"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")

    text_file = args.text_file
    if not os.path.isfile(text_file):
        print(f"File not found: {text_file}")
        sys.exit(1)

    i = 0
    named_cues = []
    for i, (chunk, speaker, group_name) in enumerate(
                              generate_chunks_from_file(
                                text_file,
                                max_sentences_per_chunk=args.max_sentences,
                                max_lines_per_chunk=args.max_lines,
                                max_chars_per_line=args.max_chars), 1):
        group_info = f" [group: {group_name}]" if group_name else ""
        print(f"Creating cue {i}: ({speaker}){group_info} {chunk}")
        cue_id = create_text_cue(chunk, speaker=speaker)
        if group_name:
            if cue_id:
                named_cues.append((cue_id, group_name))
            else:
                print(f"  Warning: could not get cue id, group '{group_name}' won't be named")

    print("Creating fade groups")
    create_fadegroups()

    if named_cues:
        print(f"Naming {len(named_cues)} groups")
        rename_groups(named_cues)

    print(f"All {i} cues created successfully!")

if __name__ == "__main__":
    main()

