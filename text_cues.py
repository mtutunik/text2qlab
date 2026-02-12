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
def create_text_cue(chunk, layer=LAYER, opacity=OPACITY):
    """
    Writes chunk to a temp file and uses AppleScript to create a Text cue.
    This avoids any quoting issues.
    """
    # Write chunk to temp text file
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
        f.write(chunk)
        text_path = f.name

    # Write AppleScript to temp file
    applescript = f'''
set chunkText to read POSIX file "{text_path}"

# make new text cue with properties {{text:chunkText, layer:{layer}, opacity:{opacity}}}

tell application id "com.figure53.QLab.5" to tell front workspace
    make type "Text"
    set selectedCues to selected as list
    set newCue to last item of (selected as list)
    set thecuenumber to q number of newCue
    set the text of newCue to chunkText
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
    max_sentences_per_chunk=2,
    max_lines_per_chunk=4,
    max_chars_per_line=80
):
    """
    Stream text from file and yield chunks progressively:
    - max_sentences_per_chunk per chunk
    - max_lines_per_chunk per chunk
    - max_chars_per_line per line
    """

    sentence_buffer = []
    text_buffer = ""

    sentence_end_re = re.compile(r'(?<!\.)[.!?](?!\.)\s+') 

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            text_buffer += line.strip() + " "

            # Extract sentences progressively
            while True:
                match = sentence_end_re.search(text_buffer)
                if not match:
                    break

                end_index = match.end()
                sentence = text_buffer[:end_index].strip()
                text_buffer = text_buffer[end_index:].strip()

                sentence_buffer.append(sentence)

                # If we reached max sentences → build chunk
                if len(sentence_buffer) >= max_sentences_per_chunk:
                    yield from _emit_chunks_from_sentences(
                        sentence_buffer,
                        max_lines_per_chunk,
                        max_chars_per_line
                    )
                    sentence_buffer = []

    # Handle remaining buffer at EOF
    if text_buffer.strip():
        sentence_buffer.append(text_buffer.strip())

    if sentence_buffer:
        yield from _emit_chunks_from_sentences(
            sentence_buffer,
            max_lines_per_chunk,
            max_chars_per_line
        )


def _emit_chunks_from_sentences(
    sentences,
    max_lines_per_chunk,
    max_chars_per_line
):
    """
    Helper that formats sentences into wrapped chunks and yields them.
    """

    chunk_text = " ".join(sentences).strip()
    lines = textwrap.wrap(chunk_text, width=max_chars_per_line)

    for i in range(0, len(lines), max_lines_per_chunk):
        sub_lines = lines[i:i + max_lines_per_chunk]
        yield "\n".join(sub_lines)


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


    
    for i, chunk in enumerate(
                              generate_chunks_from_file(
                                text_file,
                                max_sentences_per_chunk=2,
                                max_lines_per_chunk=4,
                                max_chars_per_line=80), 1):
        print(f"Creating cue {i}: {chunk}")
        create_text_cue(chunk)

    print("Creating fade groups")
    create_fadegroups()

    print(f"All {i} cues created successfully!")

if __name__ == "__main__":
    main()

