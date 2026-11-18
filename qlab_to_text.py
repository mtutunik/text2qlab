"""
Extract text from QLab Text cues and write it back to a script file.

This is the reverse of text_cues.py: it reads every Text cue in the front
QLab workspace (in cue list order, including cues nested inside groups such
as the crossfade groups made by fadegroup.applescript), takes the speaker
from each cue's notes, and writes the result as:

    SPEAKER: text text text...

    OTHER SPEAKER: text text text...

Consecutive cues with the same speaker are merged into one paragraph.

If a Text cue sits inside a group whose name is not the default one made by
fadegroup.applescript (i.e. does not start with "Crossfade"), a marker line

    [GROUP: group name]

is written before that cue's text. text_cues.py reads the marker and gives
the group it creates for that cue the same name.
"""

import argparse
import re
import subprocess
import sys
import threading

# Field / record separators used to pass data out of AppleScript safely
FIELD_SEP = "\x1f"   # ASCII unit separator
RECORD_SEP = "\x1e"  # ASCII record separator

# Prefix of the progress lines the AppleScript logs to stderr for each cue
PROGRESS_TAG = "PROGRESS"

APPLESCRIPT = r'''
global cueCount

on run argv
    set cueCount to 0
    set cueListName to ""
    if (count of argv) > 0 then set cueListName to item 1 of argv

    set FS to character id 31
    set RS to character id 30

    tell application id "com.figure53.QLab.5" to tell front workspace
        if cueListName is "" then
            set topLists to cue lists
        else
            set topLists to (cue lists whose q name is cueListName)
            if (count of topLists) is 0 then error "Cue list not found: " & cueListName
        end if
    end tell

    set cueRecords to {}
    repeat with cl in topLists
        set cueRecords to cueRecords & my collectCues(cl, FS, "", "")
    end repeat

    set AppleScript's text item delimiters to RS
    set outText to cueRecords as text
    set AppleScript's text item delimiters to ""
    return outText
end run

-- groupID / groupName describe parentCue when it is a Group cue, else ""
on collectCues(parentCue, FS, groupID, groupName)
    set found to {}
    tell application id "com.figure53.QLab.5"
        set childCues to cues of parentCue
        repeat with c in childCues
            set cueType to q type of c
            set cueCount to cueCount + 1
            log "PROGRESS" & tab & cueCount & tab & (q number of c)
            if cueType is "Text" then
                set cueText to text of c
                set cueNotes to notes of c
                set end of found to (cueText & FS & cueNotes & FS & groupID & FS & groupName)
            else if cueType is "Group" then
                set found to found & my collectCues(c, FS, uniqueID of c, q name of c)
            else if cueType is in {"Cue List", "Cart"} then
                set found to found & my collectCues(c, FS, "", "")
            end if
        end repeat
    end tell
    return found
end collectCues
'''


def fetch_text_cues(cue_list=None):
    """
    Returns a list of (text, speaker, group_id, group_name) tuples for every
    Text cue, in order. group_id / group_name are "" if the cue is not in a group.
    """
    args = ["osascript", "-", cue_list] if cue_list else ["osascript", "-"]
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    proc.stdin.write(APPLESCRIPT)
    proc.stdin.close()

    # Read stdout in the background so a large result can't block osascript
    # while we're reading progress lines from stderr.
    stdout_parts = []
    reader = threading.Thread(target=lambda: stdout_parts.append(proc.stdout.read()))
    reader.start()

    errors = []
    progress_shown = False
    for line in proc.stderr:
        fields = line.rstrip("\n").split("\t")
        if fields[0] == PROGRESS_TAG and len(fields) == 3:
            count, number = fields[1], fields[2]
            label = f"Reading cue {count}" + (f" (Q {number})" if number else "")
            # \r + clear-to-end-of-line: overwrite the same terminal line
            print(f"\r{label}\033[K", end="", flush=True)
            progress_shown = True
        else:
            errors.append(line)

    reader.join()
    proc.wait()
    if progress_shown:
        print()  # finish the progress line

    if proc.returncode != 0:
        print(f"AppleScript error: {''.join(errors).strip()}", file=sys.stderr)
        sys.exit(1)

    output = stdout_parts[0] if stdout_parts else ""
    if output.endswith("\n"):
        output = output[:-1]
    if not output:
        return []

    cues = []
    for record in output.split(RECORD_SEP):
        text, speaker, group_id, group_name = (record.split(FIELD_SEP) + ["", "", ""])[:4]
        cues.append((text, speaker.strip(), group_id, group_name))
    return cues


def fix_mojibake(text):
    """
    Repairs text that was UTF-8 but got read as Mac Roman (e.g. '—' -> '‚Äî').
    This happens when AppleScript's `read POSIX file` loads a UTF-8 file
    without `as «class utf8»`. Text that is not mojibake is returned unchanged.
    """
    try:
        return text.encode("mac_roman").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def normalize(text):
    """Collapse the line breaks / extra whitespace inside a cue into single spaces."""
    return re.sub(r"\s+", " ", fix_mojibake(text)).strip()


DEFAULT_GROUP_PREFIX = "Crossfade"


def custom_group_name(group_name):
    """Returns the group name if the user set it, or None for default/empty names."""
    name = normalize(group_name)
    if not name or name.startswith(DEFAULT_GROUP_PREFIX):
        return None
    return name


def build_paragraphs(cues):
    """
    Merges consecutive cues with the same speaker into paragraphs.
    A cue in a custom-named group always starts a new paragraph that carries
    the group name (only for the first Text cue of that group).
    Returns a list of (group_name or None, speaker, paragraph_text).
    """
    paragraphs = []
    previous_group_id = None
    for text, speaker, group_id, group_name in cues:
        text = normalize(text)
        if not text:
            continue

        group = custom_group_name(group_name)
        starts_named_group = group is not None and group_id != previous_group_id
        previous_group_id = group_id

        if paragraphs and not starts_named_group and paragraphs[-1][1] == speaker:
            paragraphs[-1][2].append(text)
        else:
            paragraphs.append((group if starts_named_group else None, speaker, [text]))
    return [(group, speaker, " ".join(parts)) for group, speaker, parts in paragraphs]


def format_script(paragraphs):
    blocks = []
    for group, speaker, text in paragraphs:
        line = f"{speaker}: {text}" if speaker else text
        if group:
            line = f"[GROUP: {group}]\n{line}"
        blocks.append(line)
    return "\n\n".join(blocks) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Extract QLab Text cues back into a 'SPEAKER: lines' script file."
    )
    parser.add_argument("output_file", help="Path of the text file to write")
    parser.add_argument(
        "--cue-list",
        help="Only extract from the cue list with this name (default: all cue lists)",
    )
    args = parser.parse_args()

    cues = fetch_text_cues(args.cue_list)
    if not cues:
        print("No Text cues found.")
        sys.exit(1)

    paragraphs = build_paragraphs(cues)
    with open(args.output_file, "w", encoding="utf-8") as f:
        f.write(format_script(paragraphs))

    print(f"Extracted {len(cues)} cues into {len(paragraphs)} paragraphs -> {args.output_file}")


if __name__ == "__main__":
    main()
