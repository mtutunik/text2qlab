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
MAX_CHARS_PER_LINE=55

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

    speaker_note = f'"{speaker}"' if speaker else ""

    applescript = f'''
set chunkText to read POSIX file "{text_path}"
set speaker_note to {speaker_note}
# make new text cue with properties {{text:chunkText, layer:{layer}, opacity:{opacity}}}

tell application id "com.figure53.QLab.5" to tell front workspace
    make type "Text"
    set selectedCues to selected as list
    set newCue to last item of (selected as list)
    set thecuenumber to q number of newCue
    set the text of newCue to chunkText
    set notes of newCue to speaker_note
end tell
'''

    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as f:
        f.write(applescript)
        script_path = f.name

    # Run the AppleScript
    subprocess.run(["osascript", script_path])
    # Clean up temp files
    os.remove(text_path)
    os.remove(script_path)


def create_fadegroups():
     subprocess.run(["osascript", "fadegroup.applescript"])


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




def generate_chunks_from_file(
    file_path,
    max_sentences_per_chunk=MAX_SENTENCES_PER_CHUNK,
    max_lines_per_chunk=MAX_LINES_PER_CHUNK,
    max_chars_per_line=MAX_CHARS_PER_LINE
):
    """
    Streaming generator that:
    - Detects speaker lines (e.g., STUDENT:, NARRATOR 2:)
    - Excludes speaker names from output
    - Starts a new chunk when speaker changes
    - Respects sentence/line/char limits
    """

    speaker_pattern = re.compile(r'^[A-Z0-9 ]+:\s*')
    sentence_split_pattern = re.compile(r'(?<!\.)[.!?](?!\.)\s+')

    current_sentences = []
    current_speaker = None

    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()

            if not line:
                continue

            # -----------------------------
            # Detect speaker
            # -----------------------------
            speaker_match = speaker_pattern.match(line)
            if speaker_match:
                # New speaker → flush current chunk first
                if current_sentences:
                    for chunk in build_chunks_from_sentences(
                        current_sentences,
                        max_sentences_per_chunk,
                        max_lines_per_chunk,
                        max_chars_per_line
                    ):
                        yield chunk, current_speaker
                    current_sentences = []

                #current_speaker = speaker_match.group().rstrip(':').strip()  # remove colon
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
        if current_sentences:
            for chunk in build_chunks_from_sentences(
                current_sentences,
                max_sentences_per_chunk,
                max_lines_per_chunk,
                max_chars_per_line
            ):
                yield chunk, current_speaker


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
def main():
    if len(sys.argv) < 2:
        print("Usage: python create_qlab_cues.py <text_file>")
        sys.exit(1)

    text_file = sys.argv[1]
    if not os.path.isfile(text_file):
        print(f"File not found: {text_file}")
        sys.exit(1)

    # Read text from file
    # with open(text_file, "r", encoding="utf-8") as f:
    #    text = f.read().replace("\n", " ")  # remove line breaks for clean splitting

    # Split into chunks
    #chunks = split_text(text, MAX_CHARS_PER_CUE)

    # Create a cue in QLab for each chunk
    #for i, chunk in enumerate(chunks, 1):
    #    print(f"Creating cue {i}: {chunk}")
    #    create_text_cue(chunk)


    
    for i, (chunk, speaker) in enumerate(
                              generate_chunks_from_file(
                                text_file,
                                max_sentences_per_chunk=MAX_SENTENCES_PER_CHUNK,
                                max_lines_per_chunk=MAX_LINES_PER_CHUNK,
                                max_chars_per_line=MAX_CHARS_PER_LINE), 1):
        print(f"Creating cue {i}: ({speaker}) {chunk}")
        create_text_cue(chunk, speaker=speaker)

    print("Creating fade groups")
    create_fadegroups()

    print(f"All {i} cues created successfully!")

if __name__ == "__main__":
    main()

