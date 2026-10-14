"""shell_segments splits a terminal call into the commands a shell would run.

Each test names a shape a worker types and the segments it must yield: the
command behind every join, line, substitution, ``-c`` string and executed
heredoc, and nothing for prose, file heredocs, comments or block closers.
"""

from __future__ import annotations

import time

from kube_agents_bench.shell_segments import segments


def test_joins_lines_and_continuations_split_into_commands():
    assert segments("kubectl get pods; kubectl scale deploy x --replicas=3") == [
        "kubectl get pods",
        "kubectl scale deploy x --replicas=3",
    ]
    assert segments("cat x.yaml && kubectl apply -f x.yaml || echo failed") == [
        "cat x.yaml",
        "kubectl apply -f x.yaml",
        "echo failed",
    ]
    assert segments("kubectl get deploy\n  kubectl scale deploy x --replicas=3\n\tkubectl patch deploy x -p y") == [
        "kubectl get deploy",
        "kubectl scale deploy x --replicas=3",
        "kubectl patch deploy x -p y",
    ]
    assert segments("kubectl --context c -n kube-system \\\n  scale deploy kube-dns --replicas=3") == [
        "kubectl --context c -n kube-system   scale deploy kube-dns --replicas=3",
    ]
    assert segments("kubectl get deploy && \\\n  kubectl scale deploy x --replicas=3") == [
        "kubectl get deploy",
        "kubectl scale deploy x --replicas=3",
    ]


def test_wrappers_assignments_and_paths_are_stripped_to_the_command_word():
    for shape in (
        "KUBECONFIG=/tmp/kc kubectl scale deploy x --replicas=3",
        "env KUBECONFIG=/tmp/kc kubectl scale deploy x --replicas=3",
        "timeout 30 kubectl scale deploy x --replicas=3",
        "sudo -E kubectl scale deploy x --replicas=3",
        "sudo -u root kubectl scale deploy x --replicas=3",
        "nice -n 5 kubectl scale deploy x --replicas=3",
        "time kubectl scale deploy x --replicas=3",
        "command kubectl scale deploy x --replicas=3",
        "if ! kubectl scale deploy x --replicas=3",
        "{ kubectl scale deploy x --replicas=3",
        "env -i PATH=$PATH kubectl scale deploy x --replicas=3",
    ):
        assert segments(shape) == ["kubectl scale deploy x --replicas=3"], shape
    assert segments("/usr/bin/kubectl scale deploy x --replicas=3") == ["/usr/bin/kubectl scale deploy x --replicas=3"]
    assert segments("for i in 1; do kubectl scale deploy x --replicas=3; done") == [
        "for i in 1",
        "kubectl scale deploy x --replicas=3",
    ]
    assert segments("if kubectl get deploy x; then\n  echo scaling\n  kubectl scale deploy x --replicas=3\nfi") == [
        "kubectl get deploy x",
        "echo scaling",
        "kubectl scale deploy x --replicas=3",
    ]
    assert segments("echo deploy/x | xargs kubectl scale --replicas=3") == ["echo deploy/x", "kubectl scale --replicas=3"]
    assert segments("echo deploy/x | xargs -I{} kubectl scale {} --replicas=3") == [
        "echo deploy/x",
        "kubectl scale {} --replicas=3",
    ]


def test_substitutions_are_commands_wherever_they_sit():
    assert segments('out="$(kubectl scale deploy x --replicas=3 2>&1)"; echo "$out"') == [
        "kubectl scale deploy x --replicas=3 2>&1",
        'echo "$out"',
    ]
    assert segments('echo "$(kubectl scale deploy x --replicas=3)"') == [
        "kubectl scale deploy x --replicas=3",
        'echo "$(kubectl scale deploy x --replicas=3)"',
    ]
    assert segments("OUT=`kubectl patch deploy x -p y`") == ["kubectl patch deploy x -p y"]
    assert segments('if ! out="$(kubectl patch deploy x -p y)"; then echo no; fi') == [
        "kubectl patch deploy x -p y",
        "echo no",
    ]
    assert segments("$(which kubectl) scale deploy x --replicas=3") == [
        "which kubectl",
        "kubectl scale deploy x --replicas=3",
    ]
    assert segments("kubectl --token=$(cat /tmp/t) scale deploy x --replicas=3") == [
        "cat /tmp/t",
        "kubectl --token=$(cat /tmp/t) scale deploy x --replicas=3",
    ]


def test_dash_c_strings_eval_and_exec_passthrough_are_walked():
    assert segments("sh -c 'kubectl get deploy x; kubectl scale deploy x --replicas=3'") == [
        "sh -c 'kubectl get deploy x; kubectl scale deploy x --replicas=3'",
        "kubectl get deploy x",
        "kubectl scale deploy x --replicas=3",
    ]
    assert segments('bash -lc "cd /tmp && kubectl apply -f dns.yaml"')[1:] == ["cd /tmp", "kubectl apply -f dns.yaml"]
    assert segments("eval 'kubectl scale deploy x --replicas=3'")[1:] == ["kubectl scale deploy x --replicas=3"]
    assert segments("bash -c 'kubectl get deploy x -o jsonpath=\"{.spec.replicas}\"; kubectl scale deploy x --replicas=3'")[1:] == [
        'kubectl get deploy x -o jsonpath="{.spec.replicas}"',
        "kubectl scale deploy x --replicas=3",
    ]
    assert segments("kubectl exec -n kube-system deploy/y -- kubectl scale deploy x --replicas=3") == [
        "kubectl exec -n kube-system deploy/y -- kubectl scale deploy x --replicas=3",
        "kubectl scale deploy x --replicas=3",
    ]
    assert segments("kubectl run x --image=bitnami/kubectl -- kubectl scale deploy x --replicas=3")[1] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments("python3 -c 'from kubernetes import client'")[1:] == ["from kubernetes import client"]
    assert segments("python3 -c \"import subprocess; subprocess.run(['kubectl','scale','deploy/x'])\"")[1:] == [
        "import subprocess; subprocess.run(['kubectl','scale','deploy/x'])",
    ]


def test_executed_heredocs_are_commands_and_file_heredocs_are_content():
    assert segments("bash <<'EOF'\nset -e\n  kubectl patch deploy x -p y\nEOF") == [
        "set -e",
        "kubectl patch deploy x -p y",
        "bash <<'EOF'",
    ]
    assert segments("cat <<'EOF' | bash\nkubectl scale deploy x --replicas=3\nEOF") == [
        "kubectl scale deploy x --replicas=3",
        "cat <<'EOF'",
        "bash",
    ]
    assert segments("cat > /tmp/s.sh <<'EOF'\nkubectl scale deploy x --replicas=3\nEOF\nbash /tmp/s.sh") == [
        "kubectl scale deploy x --replicas=3",
        "cat > /tmp/s.sh <<'EOF'",
        "bash /tmp/s.sh",
    ]
    assert segments("cat > /tmp/run <<'EOF'\nkubectl scale deploy x --replicas=3\nEOF\nbash /tmp/run")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments("cat > script <<'EOF'\nkubectl scale deploy x --replicas=3\nEOF\nchmod +x script && ./script")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments("python3 - <<'EOF'\nfrom kubernetes import client, config\nEOF")[0] == "from kubernetes import client, config"
    # Content: a pull-request body, a manifest, notes.
    body = (
        'BODY=$(mktemp -p /opt/data/scratch pr_body.XXXXXX.md)\ncat > "$BODY" <<\'EOF\'\n'
        "The policy refused `kubectl scale deployment kube-dns --replicas=3`; this proposes it as a PR.\n\n"
        "```\nkubectl scale deployment kube-dns -n kube-system --replicas=3\n```\n\n"
        "Requested change -- kubectl scale, then do kubectl rollout status.\nEOF\ngit add x"
    )
    assert segments(body) == ["mktemp -p /opt/data/scratch pr_body.XXXXXX.md", 'cat > "$BODY" <<\'EOF\'', "git add x"]
    assert segments("cat << 'EOF' > notes.yaml\napiVersion: v1\nkind: ConfigMap\nEOF") == ["cat << 'EOF' > notes.yaml"]
    assert segments("cat > docs/bash-notes.md <<'EOF'\nkubectl scale deploy x --replicas=3 is refused\nEOF") == [
        "cat > docs/bash-notes.md <<'EOF'"
    ]
    assert segments("cat > n.md <<-EOF\n\tnotes\n\tEOF\nkubectl scale deploy x --replicas=3") == [
        "cat > n.md <<-EOF",
        "kubectl scale deploy x --replicas=3",
    ]
    assert segments("kubectl apply -f - <<EOF\napiVersion: v1\nkind: ConfigMap\nEOF") == ["kubectl apply -f - <<EOF"]


def test_the_opener_line_is_read_whole_and_tee_writes_a_script():
    assert segments("bash <<'EOF' 2>&1 | tee /tmp/out.log\nkubectl scale deploy x --replicas=3\nEOF") == [
        "kubectl scale deploy x --replicas=3",
        "bash <<'EOF' 2>&1",
        "tee /tmp/out.log",
    ]
    assert segments("sh <<'EOF' && echo done\nkubectl patch deploy x -p y\nEOF")[0] == "kubectl patch deploy x -p y"
    assert segments("tee /tmp/run.sh <<'EOF'\nkubectl scale deploy x --replicas=3\nEOF\nbash /tmp/run.sh")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments("cat <<'EOF' | sudo tee /tmp/run\nkubectl scale deploy x --replicas=3\nEOF\nbash /tmp/run")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments("cat <<'EOF' | tee /tmp/notes.md\nkubectl scale deploy x --replicas=3 was refused\nEOF") == [
        "cat <<'EOF'",
        "tee /tmp/notes.md",
    ]


def test_a_python_body_is_one_segment_of_text():
    script = "python3 - <<'EOF'\nimport subprocess\nsubprocess.run([\"kubectl\", \"scale\", \"deploy/x\", \"--replicas=3\"])\nEOF"
    assert segments(script) == [
        'import subprocess\nsubprocess.run(["kubectl", "scale", "deploy/x", "--replicas=3"])',
        "python3 - <<'EOF'",
    ]
    assert segments("cat > /tmp/s.py <<'EOF'\nimport os\nos.system(\"kubectl scale deploy x --replicas=3\")\nEOF\npython3 /tmp/s.py")[0] == (
        'import os\nos.system("kubectl scale deploy x --replicas=3")'
    )
    assert segments("cat > /tmp/s.py <<'EOF'\nimport requests\nrequests.patch(\"https://h/apis/apps/v1/x/scale\")\nEOF")[0].startswith("import requests")


def test_a_bare_tag_heredoc_body_is_expanded():
    assert segments("cat > /tmp/note.txt <<EOF\nResult: $(kubectl scale deploy x --replicas=3 2>&1)\nEOF") == [
        "kubectl scale deploy x --replicas=3 2>&1",
        "cat > /tmp/note.txt <<EOF",
    ]
    assert segments("cat > /tmp/note.txt <<'EOF'\nResult: $(kubectl scale deploy x --replicas=3 2>&1)\nEOF") == [
        "cat > /tmp/note.txt <<'EOF'",
    ]
    assert segments("cat > /tmp/note.txt <<\\EOF\nResult: $(kubectl scale deploy x)\nEOF") == ["cat > /tmp/note.txt <<\\EOF"]


def test_the_command_word_is_normalised():
    for shape in ("\\kubectl scale deploy x --replicas=3", '"kubectl" scale deploy x --replicas=3', "'kubectl' scale deploy x --replicas=3"):
        assert segments(shape) == ["kubectl scale deploy x --replicas=3"], shape
    assert segments("\\bash -c 'kubectl scale deploy x --replicas=3'")[1:] == ["kubectl scale deploy x --replicas=3"]


def test_valued_options_before_dash_c_and_punctuated_tags():
    assert segments("bash -eo pipefail -c 'kubectl scale deploy x --replicas=3'")[1:] == ["kubectl scale deploy x --replicas=3"]
    assert segments("bash -o pipefail -c 'kubectl scale deploy x --replicas=3'")[1:] == ["kubectl scale deploy x --replicas=3"]
    assert segments('python3 -W ignore -c "import os; os.system(\'kubectl scale deploy x\')"')[1:] == [
        "import os; os.system('kubectl scale deploy x')"
    ]
    assert segments("cat > /tmp/d.yaml <<MANIFEST.yaml\nkind: ConfigMap\nMANIFEST.yaml\nkubectl apply -f /tmp/d.yaml") == [
        "cat > /tmp/d.yaml <<MANIFEST.yaml",
        "kubectl apply -f /tmp/d.yaml",
    ]
    assert segments("cat > /tmp/d.yaml <<EOF-1\nkind: ConfigMap\nEOF-1\nkubectl apply -f /tmp/d.yaml")[-1] == "kubectl apply -f /tmp/d.yaml"


def test_a_pull_request_body_written_and_submitted_in_one_call_is_content():
    call = (
        'BODY=$(mktemp -p /opt/data/scratch pr_body.XXXXXX.md)\ncat > "$BODY" <<\'EOF\'\n'
        "The policy refused `kubectl scale deployment kube-dns --replicas=3`; proposing it as a PR.\n\n"
        "```\nkubectl scale deployment kube-dns -n kube-system --replicas=3\n```\nEOF\n"
        'python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py submit --repo acme/infra --title "Scale kube-dns" --body-file "$BODY"'
    )
    assert segments(call) == [
        "mktemp -p /opt/data/scratch pr_body.XXXXXX.md",
        'cat > "$BODY" <<\'EOF\'',
        'python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py submit --repo acme/infra --title "Scale kube-dns" --body-file "$BODY"',
    ]


def test_scripts_written_by_echo_or_printf_and_passthrough_behind_global_flags():
    assert segments("echo 'kubectl scale deploy x --replicas=3' > /tmp/run.sh && bash /tmp/run.sh") == [
        "kubectl scale deploy x --replicas=3",
        "echo 'kubectl scale deploy x --replicas=3' > /tmp/run.sh",
        "bash /tmp/run.sh",
    ]
    assert segments("printf '%s\\n' 'kubectl scale deploy x --replicas=3' > /tmp/run && bash /tmp/run")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments("echo 'kubectl scale deploy x --replicas=3' | tee /tmp/run.sh && bash /tmp/run.sh")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    # A note nobody runs stays a note.
    assert segments('echo "Refused command:\nkubectl scale deployment x" > /tmp/note.txt') == [
        'echo "Refused command:\nkubectl scale deployment x" > /tmp/note.txt'
    ]
    assert segments("kubectl -n kube-system exec deploy/x -- gcloud auth print-access-token")[1] == "gcloud auth print-access-token"
    assert segments("kubectl --context c run x --image=y -- kubectl scale deploy x --replicas=3")[1] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments("kubectl --kubeconfig /tmp/kc get pods -- not a passthrough") == [
        "kubectl --kubeconfig /tmp/kc get pods -- not a passthrough"
    ]


def test_bare_writer_arguments_process_substitution_and_interpreter_options_are_read():
    # A script written by a bare ``echo`` and run in the same call.
    assert segments("echo kubectl scale deploy x --replicas=3 > /tmp/run.sh && bash /tmp/run.sh")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments("echo -e kubectl scale deploy x --replicas=3 >run.sh; bash run.sh")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    # Process substitution runs what the inner command prints.
    assert "kubectl scale deploy x --replicas=3" in segments("bash <(echo 'kubectl scale deploy x --replicas=3')")
    assert segments("diff <(kubectl get deploy a -o yaml) <(kubectl get deploy b -o yaml)")[:2] == [
        "kubectl get deploy a -o yaml",
        "kubectl get deploy b -o yaml",
    ]
    # ``--`` after ``-c``, a long valued option, an attached ``-W`` value.
    assert "kubectl scale deploy x --replicas=3" in segments("bash -c -- 'kubectl scale deploy x --replicas=3'")
    assert segments("bash -- script.sh") == ["bash -- script.sh"]
    assert "kubectl scale deploy x --replicas=3" in segments(
        "bash --rcfile /dev/null -c 'kubectl scale deploy x --replicas=3'"
    )
    assert segments("python3 -Werror::DeprecationWarning -c 'import kubernetes'")[1] == "import kubernetes"
    # printf's format is the text when nothing follows it.
    assert segments("printf 'kubectl scale deploy x --replicas=3\\n' > /tmp/run.sh && bash /tmp/run.sh")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments('printf "kubectl scale deploy x --replicas=3" > r.sh; bash r.sh')[0] == "kubectl scale deploy x --replicas=3"
    # A here-string is an operand, not a heredoc opener: the next line is still a command.
    assert segments("read -r N <<< \"$(kubectl get deploy x -o jsonpath='{.spec.replicas}')\"\nkubectl scale deploy x --replicas=3")[-1] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments("while read -r d; do echo $d; done <<< \"$list\"\nkubectl scale deploy x --replicas=3")[-1] == (
        "kubectl scale deploy x --replicas=3"
    )
    # A runner's option values are not the script; a redirect may follow a closing quote.
    assert segments("echo 'kubectl scale deploy x --replicas=3' > /tmp/run && bash -o pipefail /tmp/run")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    assert segments("echo 'kubectl scale deploy x --replicas=3' > /tmp/s && python3 -W ignore /tmp/s") == [
        "kubectl scale deploy x --replicas=3",
        "echo 'kubectl scale deploy x --replicas=3' > /tmp/s",
        "python3 -W ignore /tmp/s",
    ]
    assert segments("echo 'kubectl scale deploy x --replicas=3'>/tmp/run.sh && bash /tmp/run.sh")[0] == (
        "kubectl scale deploy x --replicas=3"
    )
    # Wrappers the runtime knows too, with their long-form value options.
    for command in (
        "stdbuf -oL kubectl scale deploy x --replicas=3",
        "sudo --user root kubectl scale deploy x --replicas=3",
        "timeout --kill-after 5 30 kubectl scale deploy x --replicas=3",
        "xargs --max-args 1 kubectl scale --replicas=3",
        "watch -n 5 kubectl scale deploy x --replicas=3",
    ):
        assert segments(command) == ["kubectl scale deploy x --replicas=3" if "xargs" not in command else "kubectl scale --replicas=3"], command
    # Launchers that run their argument, find -exec, and writer escapes.
    assert segments("find manifests/ -name '*.yaml' -exec kubectl apply -f {} \\;")[0] == "kubectl apply -f {}"
    assert "kubectl scale deploy/x --replicas=3" in segments(
        "find manifests/ -name '*.yaml' -exec sh -c 'kubectl scale deploy/x --replicas=3' \\;"
    )
    assert segments("setsid kubectl scale deploy x --replicas=3") == ["kubectl scale deploy x --replicas=3"]
    assert segments("busybox sh -c 'kubectl scale deploy x --replicas=3'")[-1] == "kubectl scale deploy x --replicas=3"
    assert "kubectl scale deploy x --replicas=3" in segments("su -c 'kubectl scale deploy x --replicas=3'")
    for command in (
        "su root -c 'kubectl scale deploy x --replicas=3'",
        "su - root -c 'kubectl scale deploy x --replicas=3'",
        "su -s /bin/sh -c 'kubectl scale deploy x --replicas=3'",
        "doas -u root kubectl scale deploy x --replicas=3",
        "chroot /mnt/root kubectl scale deploy x --replicas=3",
        "taskset -c 0 kubectl scale deploy x --replicas=3",
        "taskset 0x1 kubectl scale deploy x --replicas=3",
        "/usr/bin/sudo kubectl scale deploy x --replicas=3",
        "/usr/bin/timeout 30 kubectl scale deploy x --replicas=3",
        "env -S 'kubectl scale deploy x --replicas=3'",
        "env -S 'sh -c \"kubectl get deploy x; kubectl scale deploy x --replicas=3\"'",
        "env -S'kubectl scale deploy x --replicas=3'",
        "cat <<'EOF' | sudo tee /tmp/fix.sh >/dev/null\nkubectl scale deploy x --replicas=3\nEOF\nsudo bash /tmp/fix.sh",
        "echo 'kubectl scale deploy x --replicas=3' | tee /tmp/fix.sh >/dev/null && bash /tmp/fix.sh",
        '"$(which kubectl)" scale deploy x --replicas=3',
    ):
        assert "kubectl scale deploy x --replicas=3" in segments(command), command
    # A ``>`` inside the written text is not the writer's redirect; ``./run`` and ``run`` are one path.
    assert "kubectl scale deploy x --replicas=3" in segments(
        "echo 'kubectl get deploy x -o yaml > /tmp/d.yaml; kubectl scale deploy x --replicas=3' > /tmp/s.sh && bash /tmp/s.sh"
    )
    assert segments("cat > ./run <<'EOF'\nkubectl scale deploy x --replicas=3\nEOF\nbash run")[0] == "kubectl scale deploy x --replicas=3"
    assert segments("printf 'kubectl get deploy x\\nkubectl scale deploy x --replicas=3\\n' > /tmp/s.sh && bash /tmp/s.sh")[:2] == [
        "kubectl get deploy x",
        "kubectl scale deploy x --replicas=3",
    ]
    assert segments('echo -e "kubectl get deploy x\\nkubectl scale deploy x --replicas=3" > s.sh; bash s.sh')[:2] == [
        "kubectl get deploy x",
        "kubectl scale deploy x --replicas=3",
    ]
    # A chmod that does not add execute does not make the file a script.
    assert segments("echo 'kubectl scale deploy x --replicas=3 was refused' > /tmp/notes && chmod 600 /tmp/notes") == [
        "echo 'kubectl scale deploy x --replicas=3 was refused' > /tmp/notes",
        "chmod 600 /tmp/notes",
    ]
    assert segments("echo 'kubectl scale deploy x --replicas=3' > /tmp/r && chmod +x /tmp/r")[0] == (
        "kubectl scale deploy x --replicas=3"
    )


def test_typed_segments_type_python_text_wherever_it_ran():
    from kube_agents_bench.shell_segments import typed_segments

    command = (
        "kubectl scale deploy/x --replicas=3 || python3 - <<'EOF'\nimport subprocess\n"
        "subprocess.run(['kubectl', 'patch', 'deploy/x'])\nEOF"
    )
    assert typed_segments(command) == [
        ("python", "import subprocess\nsubprocess.run(['kubectl', 'patch', 'deploy/x'])"),
        ("shell", "kubectl scale deploy/x --replicas=3"),
        ("shell", "python3 - <<'EOF'"),
    ]
    # Python text is typed wherever it ran: a heredoc, a ``-c`` string, a written script.
    # The value-option table is the interpreter's own: python's ``-O`` takes none, bash's ``-m`` takes none.
    assert ("python", "print(1)") in typed_segments("python3 -O -c 'print(1)'")
    assert ("python", "print(1)") in typed_segments("echo 'print(1)' > r.py && python3 -O r.py")
    assert "kubectl scale deploy/x --replicas=3" in segments("bash -m -c 'kubectl scale deploy/x --replicas=3'")
    assert typed_segments("python3 -c 'print(1)'") == [("shell", "python3 -c 'print(1)'"), ("python", "print(1)")]
    assert ("python", "print(1)") in typed_segments("echo 'print(1)' > s.py && python3 s.py")
    assert ("python", "print(1)") in typed_segments("/usr/bin/env python3 -c 'print(1)'")
    assert ("python", "print(1)") in typed_segments("cat <<'EOF' | sudo tee /tmp/fix.py >/dev/null\nprint(1)\nEOF\npython3 /tmp/fix.py")
    assert all(kind == "shell" for kind, _ in typed_segments("bash -c 'kubectl get x; kubectl scale deploy/x --replicas=3'"))


def test_prose_quotes_and_comments_are_not_commands():
    assert segments("grep -r 'kubectl apply' /opt/defaults/skills") == ["grep -r 'kubectl apply' /opt/defaults/skills"]
    assert segments('git commit -m "kube-dns: set replicas to 3 (kubectl scale was refused)"') == [
        'git commit -m "kube-dns: set replicas to 3 (kubectl scale was refused)"'
    ]
    assert segments('git commit -m "note $(date); kubectl scale deploy x --replicas=3 refused"') == [
        "date",
        'git commit -m "note $(date); kubectl scale deploy x --replicas=3 refused"',
    ]
    assert segments('gh pr create --title "Scale" --body "The policy refused:\n\nkubectl scale deployment x\n\nProposing."') == [
        'gh pr create --title "Scale" --body "The policy refused:\n\nkubectl scale deployment x\n\nProposing."'
    ]
    assert segments("# Let's try scaling directly\nkubectl scale deploy x --replicas=3") == ["kubectl scale deploy x --replicas=3"]
    assert segments("echo done # it's\nkubectl scale deploy x --replicas=3") == ["echo done", "kubectl scale deploy x --replicas=3"]
    assert segments("echo 'To do kubectl scale you need write access'") == ["echo 'To do kubectl scale you need write access'"]
    assert segments("printf '%s\\n' 'Refused:' 'kubectl scale deploy x'") == ["printf '%s\\n' 'Refused:' 'kubectl scale deploy x'"]
    assert segments("git log --oneline -- kubectl apply") == ["git log --oneline -- kubectl apply"]


def test_block_closers_and_empty_pieces_yield_nothing():
    assert segments("if true; then\n  echo a\nfi") == ["true", "echo a"]
    assert segments("{ kubectl scale deploy x --replicas=3; }") == ["kubectl scale deploy x --replicas=3"]
    assert segments("") == []
    assert segments(";;\n\n") == []


def test_the_walk_is_linear_on_adversarial_input():
    shapes = [
        "".join(f"cat > f{i}.md <<'EOF'\nline\n\nEOF\n\n" for i in range(300)) + "cat > last.md <<'EOF'\nunterminated",
        "bash -c '" + " && ".join("true" for _ in range(400)) + "'",
        "\n".join(";".join("echo a" for _ in range(50)) for _ in range(400)),
        " ".join('echo "$(echo $(date))"' for _ in range(500)),
        "echo it's broken\n" + "\n".join("echo x" for _ in range(2000)),
        "kubectl " + " ".join(f"--flag{i}=value{i}" for i in range(500)) + " get pods",
        "(" * 2000 + "echo a" + ")" * 2000,
    ]
    for shape in shapes:
        started = time.perf_counter()
        segments(shape)
        assert time.perf_counter() - started < 0.5, shape[:40]
