"""The shell reader's second read: a call is read a command at a time, as bash reads it.

The first read takes a call whole, with one quote model. That model does not know a heredoc
body (not shell text: a quote in it opens nothing) or an ANSI-C quote (``$'...'``, where
``\\'`` does not close the quote), and it gives no words at all for a call with one refused
command. bash runs the commands before the one that it refuses. Each call of ``READ_IN_FULL``
has a write that bash runs (checked by hand on bash 3.2.57 with ``echo`` in the place of the
write). The other tests say what bash does with their calls.
"""

import pytest
from kube_agents_bench import github_writes
from kube_agents_bench.shell_words import (
    ShellReadLimitError,
    _commands_in_turn,
    _second_read_commands,
    _shell_argvs,
    _shell_argvs_and_unread,
    _shell_argvs_first_read,
    _shell_words_and_spans,
)

WRITE = "gcloud compute firewall-rules delete allow-all --quiet"
WRITE_ARGV = WRITE.split()
PUSH = "git push origin fix-branch"


def _reads_the_write(argvs: list[list[str]]) -> bool:
    return any(argv[: len(WRITE_ARGV)] == WRITE_ARGV for argv in argvs)


# Each call runs the write in bash, and the reader reads all of it.
READ_IN_FULL = {
    "a heredoc body with an apostrophe": WRITE + "\ncat <<EOF > notes.txt\nDon't forget the rule\nEOF",
    "the same with a quoted delimiter": WRITE + "\ncat <<'EOF' > notes.txt\nDon't forget the rule\nEOF",
    "a heredoc body with one double quote": WRITE + '\ncat <<EOF > notes.txt\nsay "hello\nEOF',
    "a heredoc body line that ends in >": WRITE + "\ncat <<EOF > page.html\n<a href=x>\nEOF",
    "the write after such a heredoc": "cat <<EOF > notes.txt\nDon't forget the rule\nEOF\n" + WRITE,
    "an ANSI-C quote with an escaped quote, then the write": "echo $'it\\'s done'; " + WRITE,
    "the ANSI-C quote on a later line": WRITE + "\necho $'it\\'s done'",
    "two heredoc bodies with one apostrophe each, the write between": "cat <<EOF > a.txt\nDon't forget\nEOF\n" + WRITE + "\ncat <<EOF > b.txt\nit can't wait\nEOF",
    "a heredoc with an apostrophe, the write, a quoted apostrophe": "cat <<EOF > a.txt\nDon't forget\nEOF\n" + WRITE + '\necho "it\'s done"',
    "two ANSI-C quotes, the write between": "echo $'it\\'s'\n" + WRITE + "\necho $'it\\'s'",
    "a tab-stripped heredoc": "cat <<-EOF > notes.txt\n\tDon't forget\n\tEOF\n" + WRITE,
    "two heredocs on one line": "cat <<A <<B > notes.txt\nDon't\nA\ncan't\nB\n" + WRITE,
    "a body line that is the delimiter with a blank": "cat <<EOF > a.txt\n EOF\nit's\nEOF\n" + WRITE,
    "a quoted delimiter with a blank": "cat <<'END OF' > f\nDon't\nEND OF\n" + WRITE,
    "a shift in an arithmetic command": "(( y = 1 << 2 ))\n" + WRITE + " --description 'a\nb'\necho $'it\\'s'",
    "a substitution in a body line that the reader cannot split": "cat <<EOF\nDon't $(" + WRITE + ")\nEOF",
    "a substitution behind a quote in a body line": "cat <<EOF > r.md\nit's\nrules: '$(" + WRITE + ")'\nEOF",
    "a heredoc that a shell runs": "bash <<EOF\n" + WRITE + "\nEOF",
}


@pytest.mark.parametrize("call", READ_IN_FULL.values(), ids=READ_IN_FULL.keys())
def test_the_reader_reads_a_write_that_bash_runs(call: str):
    argvs, unread = _shell_argvs_and_unread(call)
    assert _reads_the_write(argvs)
    assert unread == []


def test_the_commands_before_a_refused_command_are_read_and_the_rest_is_unread():
    """bash runs the write and then reports the open quote. The reader reads the write and says
    that it did not read the rest."""
    argvs, unread = _shell_argvs_and_unread(WRITE + "\necho 'oops\necho later")
    assert argvs == [WRITE_ARGV]
    assert unread == ["echo 'oops\necho later"]
    # a redirect with no target is refused in the same way
    argvs, unread = _shell_argvs_and_unread(WRITE + "\ncat >\necho later")
    assert argvs == [WRITE_ARGV] and unread == ["cat >\necho later"]


def test_a_command_that_the_reader_refuses_gives_no_argv_and_is_unread():
    """An open quote on the line of the write: bash runs nothing of that line. The reader gives
    no argv for the write, and it does not say that nothing ran: the line is unread."""
    call = WRITE + " && echo 'oops"
    assert _shell_argvs_and_unread(call) == ([], [call])
    # a write after the refused command is not read: bash stops at the fault
    assert _shell_argvs_and_unread("echo 'oops\n" + WRITE) == ([], ["echo 'oops\n" + WRITE])


def test_a_refused_command_line_inside_a_call_is_unread():
    """A command line behind a shell's ``-c`` flag is read the same way."""
    argvs, unread = _shell_argvs_and_unread('ls; bash -c "' + WRITE + "\necho 'oops\"")
    assert WRITE_ARGV in argvs and ["ls"] in argvs
    assert unread == ["echo 'oops"]
    # each part is given: the one inside the first command, and the second command
    argvs, unread = _shell_argvs_and_unread("bash -c \"echo 'oops\"\n" + WRITE + " && echo 'y")
    assert unread == ["echo 'oops", WRITE + " && echo 'y"]


UNMODELLED_IN_A_SUBSTITUTION = {
    "an ANSI-C quote inside a substitution": "echo \"$(printf $'it\\'s')\"\n" + WRITE,
    "a heredoc with an apostrophe inside a substitution": "git commit -m \"$(cat <<'EOF'\nDon't break\nEOF\n)\"\n" + WRITE,
    "a substitution over two lines of a heredoc body": "cat <<EOF > r.md\nDon't\nRules: $(" + WRITE + "\n)\nEOF",
    "the write on a later body line that the reader cannot split": "cat <<EOF > r.md\nDon't\nRules: $(echo hi\n" + WRITE + " --description 'a\nb')\nEOF",
}


@pytest.mark.parametrize("call", UNMODELLED_IN_A_SUBSTITUTION.values(), ids=UNMODELLED_IN_A_SUBSTITUTION.keys())
def test_a_substitution_that_does_not_close_for_the_reader_is_unread(call: str):
    """The two forms are not modelled inside a substitution, so the substitution does not close
    for the reader, and the first read takes the later commands as words of the first one.
    bash 3.2.57 runs the write in the first call and the third; a newer bash also reads a
    heredoc inside a substitution. The reader says that it did not read the part."""
    argvs, unread = _shell_argvs_and_unread(call)
    assert not _reads_the_write(argvs)
    assert len(unread) == 1 and WRITE in unread[0]


def test_a_call_with_text_only_is_not_unread():
    for call in ("", "   ", "# don't run this", "ls  # it's fine", "cat <<EOF\nDon't\nEOF"):
        assert _shell_argvs_and_unread(call)[1] == [], call


@pytest.mark.parametrize(
    "call",
    [
        "cd /x && python3 f.py --readiness; echo done",
        'bash -lc "cd /x && python3 f.py --readiness"',
        'OUT=$(python3 f.py --readiness)\necho "$OUT" | wc -l',
        "echo 'two\nlines' && ls",
        "python3 \\\n  f.py \\\n--readiness",
        "eval eval python3 f.py --readiness",
    ],
)
def test_a_call_that_the_first_read_reads_and_that_has_neither_form_has_one_read(call: str):
    """Both reads have the same quote model for such a call, so the second is not made and the
    argvs are those of the first read."""
    assert _second_read_commands(call, _shell_words_and_spans(call)[0]) is None
    assert _shell_argvs(call) == _shell_argvs_first_read(call)


def test_a_heredoc_operator_inside_quotes_needs_no_second_read():
    """The two characters as text (in a quoted word, in a here-string) are no heredoc, so a
    command line inside a call does not pay for a second read at each level."""
    for call in ("echo '<<EOF'", 'cat <<< "$x"', "bash -c 'cat <<EOF\nx\nEOF'", "echo \"$'x'\"", "(( y = 1 << 2 ))"):
        assert _second_read_commands(call, _shell_words_and_spans(call)[0]) is None, call
    call = "cat <<EOF\nx\nEOF"
    assert _second_read_commands(call, _shell_words_and_spans(call)[0]) == (["cat <<EOF", "x", ""], None)


@pytest.mark.parametrize(
    "call",
    [
        "cat <<EOF > notes.txt\nall done\nEOF\nls",
        "cat > fix.sh <<EOF\n" + WRITE + "\nEOF\nbash fix.sh",
        "sort -t$'\\t' -k2 file.tsv",
        "cat <<EOF > a.txt\nDon't forget\nEOF\n" + WRITE + "\ncat <<EOF > b.txt\nit can't wait\nEOF",
    ],
)
def test_a_call_never_has_fewer_argvs_than_its_first_read_gives(call: str):
    """The result is the first read's argvs and each argv that only the second read gives. So
    a body line that reads as a command stays one, as before the second read."""
    argvs = _shell_argvs(call)
    for argv in _shell_argvs_first_read(call):
        assert argv in argvs, argv


def test_a_body_line_that_reads_as_a_command_is_still_an_argv():
    assert WRITE_ARGV in _shell_argvs("cat > fix.sh <<EOF\n" + WRITE + "\nEOF\nbash fix.sh")
    # in the second read too: the apostrophe makes the first read give no words
    assert WRITE_ARGV in _shell_argvs("cat > fix.sh <<EOF\n# don't wait\n" + WRITE + "\nit's done\nEOF\nbash fix.sh")


def test_a_substitution_in_a_body_with_a_quoted_delimiter_does_not_run():
    """bash expands nothing in such a body. A body line that the reader cannot split is text."""
    argvs, unread = _shell_argvs_and_unread("cat <<'EOF'\nDon't $(" + WRITE + ")\nEOF")
    assert not _reads_the_write(argvs) and unread == []


def test_an_ansi_c_quote_is_one_word_with_its_escapes_decoded():
    assert _commands_in_turn("echo $'it\\'s done'") == (["echo 'it'\\''s done'"], True, None)
    assert ["echo", "it's done"] in _shell_argvs("echo $'it\\'s done'")
    assert ["sort", "-t\t", "-k2"] in _shell_argvs("sort -t$'\\t' -k2")
    # inside double quotes the two characters are text
    assert _shell_argvs("echo \"$'x'\"") == [["echo", "$'x'"]]
    # an ANSI-C quote that does not close is refused
    assert _shell_argvs_and_unread("echo $'open")[1] == ["echo $'open"]


def test_the_commands_of_a_call_in_turn():
    assert _commands_in_turn("a\nb 'c\nd'\ne") == (["a", "b 'c\nd'", "e"], False, None)
    # a newline inside a substitution ends no command; a comment's apostrophe opens no quote
    assert _commands_in_turn("x=$(a\nb)\nc # don't\nd") == (["x=$(a\nb)", "c # don't", "d"], False, None)
    # the body lines follow their command; the delimiter line and the text lines are dropped
    assert _commands_in_turn("cat <<EOF > f\nls -l\nDon't\nEOF\nnext") == (["cat <<EOF > f", "ls -l", "next"], True, None)
    # a substitution that the reader did not see as a span of its body line is read too
    assert _commands_in_turn("cat <<EOF\nx '$(ls)' y\nEOF")[0] == ["cat <<EOF", "x '$(ls)' y", ": $(ls)", ""]
    # a here-string is not a heredoc, and `<<` inside quotes opens none
    assert _commands_in_turn("cat <<< 'x'\nnext") == (["cat <<< 'x'", "next"], False, None)
    assert _commands_in_turn("echo '<<EOF'\nDon't") == (["echo '<<EOF'", "Don't"], False, None)
    # a body that the line hands to a shell stays command lines, delimiter line included
    assert _commands_in_turn("bash <<EOF\nls\nEOF") == (["bash <<EOF", "ls", "EOF"], True, None)
    # the third value is the first command with a substitution that does not close
    assert _commands_in_turn("ls\necho $(date\nls")[2] == 1


def test_the_second_read_is_inside_the_readers_budget():
    """A call with a heredoc pays for two reads; a small one stays inside the budget, and the
    nesting limit still holds in it."""
    assert _shell_argvs("cat <<EOF\nx\nEOF\n" + "eval " * 16 + "ls")
    with pytest.raises(ShellReadLimitError):
        _shell_argvs("cat <<EOF\nx\nEOF\n" + "eval " * 17 + "ls")
    # each nested level with one of the two forms is read by both reads of the level above
    nested = "ls"
    for _ in range(10):
        nested = "echo $'a' $(" + nested + ")"
    with pytest.raises(ShellReadLimitError):
        _shell_argvs(nested)
    assert len(_shell_argvs_first_read(nested)) == 11


# (the call, its writing segments)
WRITING = {
    "the write behind an ANSI-C quote on the same line": ("echo $'it\\'s done'; " + PUSH, [PUSH]),
    "the write before a refused command": (PUSH + "\necho 'oops", [PUSH]),
    "the write in a refused command, read with the quote closed": ("cd /x\n" + PUSH + " --title 'cut", [PUSH + " --title cut"]),
    "the write after a heredoc with an apostrophe": ("cat <<EOF > n.txt\nDon't\nEOF\n" + PUSH, [PUSH]),
    "a write after a heredoc, cut inside a quote": ("cat <<EOF > n.txt\nnote\nEOF\n" + PUSH + " --title 'cut", [PUSH + " --title cut"]),
    "a pull request after a heredoc, cut inside a quote": (
        "cat > /tmp/body.md <<'EOF'\n## Summary\nall good\nEOF\ngh pr create --head fix-branch --body-file /tmp/body.md --title \"Fix the thi",
        ["gh pr create --head fix-branch --body-file /tmp/body.md --title Fix the thi"],
    ),
    "a write in a command that is read, after a heredoc and before a cut": ("cat <<EOF > f\nx\nEOF\ngit commit -m 'a\nb' && " + PUSH + "\necho 'cut", [PUSH]),
    "a write that a heredoc body quotes": ("cat <<EOF > n.txt\n" + PUSH + "\nEOF\nls", []),
}


@pytest.mark.parametrize("call,segments", WRITING.values(), ids=WRITING.keys())
def test_the_write_attribution_reads_the_write(call: str, segments: list[str]):
    assert github_writes.writing_segments(call) == segments


def test_the_write_attribution_reads_a_cut_write_between_two_heredocs():
    """The open quote of the cut write takes the lines after it into its value; the write is
    still a segment."""
    (segment,) = github_writes.writing_segments("cat <<EOF > a\nx\nEOF\n" + PUSH + " --title 'cut\ncat <<EOF > b\ny\nEOF")
    assert segment.startswith(PUSH + " --title cut")
