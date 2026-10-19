import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scratchpad_verifier.cli import main
from scratchpad_verifier.shell import analyze_bash, analyze_powershell, script_outputs
from scratchpad_verifier.transcript import analyze, output_dir_for, reconstruct, versioned_names


def tool_use(tid, name, inp, ts, cwd=None):
    return {
        "type": "assistant",
        "timestamp": ts,
        **({"cwd": cwd} if cwd else {}),
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp},
        ]},
    }


def tool_result(tid, ts, result=None, is_error=False, text="ok"):
    rec = {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": is_error},
        ]},
    }
    if result is not None:
        rec["toolUseResult"] = result
    return rec


def write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


SP = "/tmp/claude-0/proj/sess/scratchpad/notes.md"


class ReconstructTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.transcript = self.dir / "session.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_then_edits(self):
        write_jsonl(self.transcript, [
            tool_use("t1", "Write", {"file_path": SP, "content": "a\nb\nc\n"}, "2026-01-01T00:00:01Z"),
            tool_result("t1", "2026-01-01T00:00:02Z"),
            tool_use("t2", "Edit", {"file_path": SP, "old_string": "b", "new_string": "B"}, "2026-01-01T00:00:03Z"),
            tool_result("t2", "2026-01-01T00:00:04Z"),
            tool_use("t3", "MultiEdit", {"file_path": SP, "edits": [
                {"old_string": "a", "new_string": "A"},
                {"old_string": "c", "new_string": "C"},
            ]}, "2026-01-01T00:00:05Z"),
            tool_result("t3", "2026-01-01T00:00:06Z"),
            "not json is ignored",
        ])
        states = reconstruct(self.transcript)
        self.assertEqual(states[SP].content, "A\nB\nC\n")
        self.assertEqual(states[SP].versions, ["a\nb\nc\n", "a\nB\nc\n", "A\nB\nC\n"])
        self.assertTrue(states[SP].complete)

    def test_filters_non_scratch_paths(self):
        write_jsonl(self.transcript, [
            tool_use("t1", "Write", {"file_path": "/home/me/app.py", "content": "x"}, "1"),
            tool_use("t2", "Write", {"file_path": "/tmp/out.txt", "content": "y"}, "2"),
        ])
        self.assertEqual(list(reconstruct(self.transcript)), ["/tmp/out.txt"])
        self.assertEqual(len(reconstruct(self.transcript, match_all=True)), 2)
        self.assertIn("/home/me/app.py", reconstruct(self.transcript, prefixes=["/home/me/"]))

    def test_failed_write_is_ignored(self):
        write_jsonl(self.transcript, [
            tool_use("t1", "Write", {"file_path": "/tmp/f", "content": "good"}, "1"),
            tool_result("t1", "2"),
            tool_use("t2", "Write", {"file_path": "/tmp/f", "content": "bad"}, "3"),
            tool_result("t2", "4", is_error=True),
        ])
        self.assertEqual(reconstruct(self.transcript)["/tmp/f"].content, "good")

    def test_edit_uses_original_file_from_result(self):
        write_jsonl(self.transcript, [
            tool_use("t1", "Edit", {"file_path": "/tmp/f", "old_string": "x", "new_string": "y"}, "1"),
            tool_result("t1", "2", result={"filePath": "/tmp/f", "originalFile": "x x"}),
        ])
        st = reconstruct(self.transcript)["/tmp/f"]
        self.assertEqual(st.content, "y x")
        self.assertEqual(st.versions, ["x x", "y x"])

    def test_edit_without_base_is_unknown(self):
        write_jsonl(self.transcript, [
            tool_use("t1", "Edit", {"file_path": "/tmp/f", "old_string": "x", "new_string": "y"}, "1"),
        ])
        st = reconstruct(self.transcript)["/tmp/f"]
        self.assertIsNone(st.content)
        self.assertFalse(st.complete)

    def test_subagent_transcripts_included(self):
        write_jsonl(self.transcript, [])
        sub = self.dir / "session" / "subagents"
        sub.mkdir(parents=True)
        write_jsonl(sub / "agent-1.jsonl", [
            tool_use("s1", "Write", {"file_path": "/tmp/sub.txt", "content": "hi"}, "1"),
        ])
        self.assertIn("/tmp/sub.txt", reconstruct(self.transcript))
        self.assertNotIn("/tmp/sub.txt", reconstruct(self.transcript, include_subagents=False))


class LayoutTests(unittest.TestCase):
    def test_output_dir_for(self):
        self.assertEqual(str(output_dir_for(SP)), "scratchpad")
        self.assertEqual(str(output_dir_for("/tmp/x/scratchpad/sub/a.sh")), "scratchpad/sub")
        self.assertEqual(str(output_dir_for("/tmp/build/a.sh")), "tmp/build")
        self.assertEqual(str(output_dir_for("/tmp/a.sh")), "tmp")
        self.assertEqual(str(output_dir_for("/home/me/a.sh")), "other/home/me")

    def test_versioned_names(self):
        self.assertEqual(versioned_names("/tmp/a.sh", 3), ["a_00.sh", "a_01.sh", "a_02.sh"])
        self.assertEqual(versioned_names("/tmp/Makefile", 1), ["Makefile_00"])
        self.assertEqual(versioned_names("/tmp/.env", 1), [".env_00"])
        self.assertEqual(versioned_names("/tmp/a.py", 101)[-1], "a_100.py")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.transcript = self.dir / "session.jsonl"
        write_jsonl(self.transcript, [
            tool_use("t1", "Write", {"file_path": SP, "content": "hello\n"}, "1"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(list(argv))
        return rc, buf.getvalue()

    def test_show(self):
        rc, out = self.run_cli("show", str(self.transcript), SP)
        self.assertEqual((rc, out), (0, "hello\n"))

    def test_extract_versions_layout(self):
        write_jsonl(self.transcript, [
            tool_use("t1", "Write", {"file_path": "/tmp/run/a.sh", "content": "echo 1\n"}, "1"),
            tool_use("t2", "Edit", {"file_path": "/tmp/run/a.sh", "old_string": "1", "new_string": "2"}, "2"),
            tool_use("t3", "Write", {"file_path": "/tmp/run/a.sh", "content": "echo 3\n"}, "3"),
            tool_use("t4", "Write", {"file_path": SP, "content": "hello\n"}, "4"),
        ])
        out_dir = self.dir / "out"
        rc, _ = self.run_cli("extract", str(self.transcript), "-o", str(out_dir))
        self.assertEqual(rc, 0)
        session = out_dir / "session"
        self.assertEqual((session / "tmp/run/a_00.sh").read_text(), "echo 1\n")
        self.assertEqual((session / "tmp/run/a_01.sh").read_text(), "echo 2\n")
        self.assertEqual((session / "tmp/run/a_02.sh").read_text(), "echo 3\n")
        self.assertEqual((session / "scratchpad/notes_00.md").read_text(), "hello\n")
        files = [p for p in session.rglob("*.*") if p.name != "manifest.json"]
        self.assertEqual(len(files), 4)

    def test_extract_errored_calls(self):
        bad = {"file_path": "/tmp/run/a.sh", "old_string": "nope", "new_string": "x"}
        write_jsonl(self.transcript, [
            tool_use("t1", "Write", {"file_path": "/tmp/run/a.sh", "content": "echo 1\n"}, "1"),
            tool_use("t2", "Edit", bad, "2"),
            tool_result("t2", "3", is_error=True, text="String to replace not found in file."),
            tool_use("t3", "Write", {"file_path": "/tmp/never.txt", "content": "x"}, "4"),
            tool_result("t3", "5", is_error=True, text="Permission denied"),
        ])
        out_dir = self.dir / "out"
        self.run_cli("extract", str(self.transcript), "-o", str(out_dir))
        session = out_dir / "session"
        self.assertEqual(sorted(p.name for p in (session / "tmp/run").iterdir()), ["a_00.sh"])
        rec = json.loads((session / "errored/tmp/run/a_00.sh.json").read_text())
        self.assertEqual(rec["tool"], "Edit")
        self.assertEqual(rec["input"], bad)
        self.assertEqual(rec["error"], "String to replace not found in file.")
        rec = json.loads((session / "errored/tmp/never_00.txt.json").read_text())
        self.assertEqual(rec["error"], "Permission denied")
        self.assertFalse((session / "tmp/never_00.txt").exists())

    def test_extract_errored_bash_and_other_tools(self):
        write_jsonl(self.transcript, [
            tool_use("b1", "Bash", {"command": "pytest", "description": "run tests"}, "1"),
            tool_result("b1", "2", is_error=True, text="Exit code 1\n2 failed"),
            tool_use("b2", "Bash", {"command": "ls"}, "3"),
            tool_result("b2", "4", text="a b"),
            tool_use("b3", "Bash", {"command": "make"}, "5"),
            tool_result("b3", "6", is_error=True, text="Exit code 2"),
            tool_use("m1", "mcp__github__get_me", {}, "7"),
            tool_result("m1", "8", is_error=True, text=[{"type": "text", "text": "401"}]),
            tool_use("w1", "Write", {"file_path": "/home/me/app.py", "content": "x"}, "9"),
            tool_result("w1", "10", is_error=True, text="denied"),
        ])
        out_dir = self.dir / "out"
        self.run_cli("extract", str(self.transcript), "-o", str(out_dir))
        err = out_dir / "session" / "errored"
        self.assertEqual(sorted(p.name for p in (err / "bash").iterdir()), ["bash_00.json", "bash_01.json"])
        rec = json.loads((err / "bash/bash_00.json").read_text())
        self.assertEqual(rec["input"]["command"], "pytest")
        self.assertEqual(rec["error"], "Exit code 1\n2 failed")
        self.assertEqual(json.loads((err / "bash/bash_01.json").read_text())["input"]["command"], "make")
        rec = json.loads((err / "mcp__github__get_me/mcp__github__get_me_00.json").read_text())
        self.assertEqual(rec["error"], "401")
        # Failed file writes are kept with the file, wherever it lives.
        rec = json.loads((err / "other/home/me/app_00.py.json").read_text())
        self.assertEqual(rec["error"], "denied")

        rc, out = self.run_cli("list", str(self.transcript))
        self.assertIn("4 other errored tool call(s)", out)
        self.assertIn("pytest", out)

    def test_verify(self):
        root = self.dir / "root"
        disk = root / SP.lstrip("/")
        disk.parent.mkdir(parents=True)
        disk.write_text("hello\n")
        rc, out = self.run_cli("verify", str(self.transcript), "--root", str(root))
        self.assertEqual(rc, 0)
        self.assertIn("MATCH", out)

        disk.write_text("changed\n")
        rc, out = self.run_cli("verify", str(self.transcript), "--root", str(root))
        self.assertEqual(rc, 1)
        self.assertIn("DIFFERS", out)

    def test_list_json(self):
        rc, out = self.run_cli("list", str(self.transcript), "--json")
        data = json.loads(out)["files"]
        self.assertEqual(data[0]["path"], SP)
        self.assertEqual(data[0]["status"], "ok")


class ShellReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.transcript = self.dir / "session.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def bash(self, tid, command, ts, cwd="/repo", error=None):
        return [
            tool_use(tid, "Bash", {"command": command}, ts, cwd=cwd),
            tool_result(tid, ts + "b", is_error=error is not None, text=error or "ok"),
        ]

    def test_heredoc_then_edit(self):
        write_jsonl(self.transcript, [
            *self.bash("b1", "cd /tmp/w && cat > run.sh <<'EOF'\necho 1\nEOF\nbash run.sh", "1"),
            tool_use("e1", "Edit", {"file_path": "/tmp/w/run.sh", "old_string": "1", "new_string": "2"}, "2"),
            tool_result("e1", "2b"),
        ])
        st = reconstruct(self.transcript)["/tmp/w/run.sh"]
        self.assertEqual(st.versions, ["echo 1\n", "echo 2\n"])
        self.assertEqual([v.how for v in st.history], ["heredoc", "Edit"])

    def test_failed_command_still_writes_heredoc(self):
        write_jsonl(self.transcript, [
            *self.bash("b1", "cat > /tmp/t.py <<'EOF'\nraise SystemExit(1)\nEOF\npython /tmp/t.py",
                       "1", error="Exit code 1"),
        ])
        self.assertEqual(reconstruct(self.transcript)["/tmp/t.py"].content, "raise SystemExit(1)\n")

    def test_denied_command_is_not_replayed(self):
        write_jsonl(self.transcript, [
            *self.bash("b1", "echo hi > /tmp/x.txt", "1",
                       error="The user doesn't want to proceed with this tool use."),
        ])
        self.assertNotIn("/tmp/x.txt", reconstruct(self.transcript))

    def test_ran_from_root(self):
        write_jsonl(self.transcript, [
            tool_use("w1", "Write", {"file_path": "/repo/check_tmp.py", "content": "print(1)\n"}, "1", cwd="/repo"),
            tool_result("w1", "1b", result={"type": "create", "originalFile": None}),
            tool_use("w2", "Write", {"file_path": "/repo/src/app.py", "content": "app\n"}, "2", cwd="/repo"),
            tool_result("w2", "2b", result={"type": "create", "originalFile": None}),
            *self.bash("b1", "python check_tmp.py && rm check_tmp.py", "3"),
        ])
        session = analyze(self.transcript)
        self.assertEqual(session.files["/repo/check_tmp.py"].category, "ran_from_root")
        self.assertEqual(session.files["/repo/src/app.py"].category, "other")
        out = self.dir / "out"
        with redirect_stdout(io.StringIO()):
            main(["extract", str(self.transcript), "-o", str(out)])
        self.assertEqual((out / "session/ran_from_root/check_tmp_00.py").read_text(), "print(1)\n")
        self.assertFalse((out / "session/other").exists())
        self.assertIn("python check_tmp.py", (out / "session/commands/bash_0000.sh").read_text())

    def test_inline_scripts_and_produced_files(self):
        write_jsonl(self.transcript, [
            *self.bash("b1", "S=/tmp/s/scratchpad; cd $S && python3 - <<'PY'\n"
                       "import json\njson.dump({}, open('out.json', 'w'))\nPY", "1"),
        ])
        session = analyze(self.transcript)
        self.assertEqual(len(session.scripts), 1)
        self.assertIn("/tmp/s/scratchpad/out.json", session.produced)
        out = self.dir / "out"
        with redirect_stdout(io.StringIO()):
            main(["extract", str(self.transcript), "-o", str(out)])
        code = (out / "session/inline_scripts/python3_0000.py").read_text()
        self.assertIn("json.dump", code)
        manifest = json.loads((out / "session/manifest.json").read_text())
        self.assertEqual(manifest["produced_without_content"][0]["path"], "/tmp/s/scratchpad/out.json")

    def test_cp_copies_known_content(self):
        write_jsonl(self.transcript, [
            tool_use("w1", "Write", {"file_path": "/tmp/s/scratchpad/a.md", "content": "A"}, "1"),
            *self.bash("b1", "mkdir -p /tmp/deliver && cp /tmp/s/scratchpad/a.md /tmp/deliver", "2"),
        ])
        self.assertEqual(reconstruct(self.transcript)["/tmp/deliver/a.md"].content, "A")

    def test_windows_paths_merge(self):
        win = "C:\\Users\\P\\AppData\\Local\\Temp\\claude\\x\\scratchpad\\a.md"
        write_jsonl(self.transcript, [
            tool_use("w1", "Write", {"file_path": win, "content": "one\n"}, "1", cwd="C:\\proj"),
            *self.bash("b1", "echo two >> /c/Users/P/AppData/Local/Temp/claude/x/scratchpad/a.md", "2",
                       cwd="C:\\proj"),
        ])
        files = reconstruct(self.transcript)
        st = files["C:/Users/P/AppData/Local/Temp/claude/x/scratchpad/a.md"]
        self.assertEqual(st.versions, ["one\n", "one\ntwo\n"])
        self.assertEqual(str(output_dir_for(st.path, st.category)), "scratchpad")

    def test_stream_json_events(self):
        events = [
            {"created_at": "1", "assistant": {"internal_anthropic_catchall": {
                "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Write",
                                         "input": {"file_path": "/tmp/e.txt", "content": "e"}}]}}}},
            {"created_at": "2", "user": {"internal_anthropic_catchall": {
                "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
                "tool_use_result": {"type": "create"}}}},
        ]
        write_jsonl(self.transcript, events)
        self.assertEqual(reconstruct(self.transcript)["/tmp/e.txt"].content, "e")


    def test_read_supplies_base_for_edits(self):
        f = "/tmp/s/scratchpad/r.py"
        write_jsonl(self.transcript, [
            tool_use("r1", "Read", {"file_path": f}, "1"),
            tool_result("r1", "1b", result={"type": "text", "file": {
                "filePath": f, "content": "a = 1\n", "startLine": 1, "numLines": 1, "totalLines": 1}}),
            tool_use("e1", "Edit", {"file_path": f, "old_string": "1", "new_string": "2"}, "2"),
            tool_result("e1", "2b", result={"originalFile": None}),
            *self.bash("b1", f"echo 'b = 3' >> {f}", "3"),
            tool_use("r2", "Read", {"file_path": f}, "4"),
            tool_result("r2", "4b", result={"type": "text", "file": {
                "filePath": f, "content": "a = 9\n", "startLine": 1, "numLines": 1, "totalLines": 1}}),
        ])
        st = reconstruct(self.transcript)[f]
        self.assertEqual(st.versions, ["a = 1\n", "a = 2\n", "a = 2\nb = 3\n", "a = 9\n"])
        self.assertEqual(st.history[-1].how, "Read")

    def test_edit_without_base_written_as_patch(self):
        f = "/tmp/s/scratchpad/p.md"
        write_jsonl(self.transcript, [
            tool_use("e1", "Edit", {"file_path": f, "old_string": "x", "new_string": "y"}, "1"),
            tool_result("e1", "1b", result={"originalFile": None, "structuredPatch": [
                {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-x", "+y"]}]}),
        ])
        out = self.dir / "out"
        with redirect_stdout(io.StringIO()):
            main(["extract", str(self.transcript), "-o", str(out)])
        patch = (out / "session/scratchpad/p_00.md.patch").read_text()
        self.assertIn("-x\n+y", patch)

    def test_names_from_output_text_and_notifications(self):
        sp = "/tmp/s/scratchpad"
        write_jsonl(self.transcript, [
            *self.bash("b1", "python run.py", "1"),
            {"type": "user", "timestamp": "1c", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "b1", "content": f"saved {sp}/report.html"}]}},
            {"type": "assistant", "timestamp": "2", "message": {"role": "assistant", "content": [
                {"type": "text", "text": f"Results are in `{sp}/summary.md`."}]}},
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "3",
             "content": f"<task-notification>output: {sp}/agent_out.txt</task-notification>"},
        ])
        produced = analyze(self.transcript).produced
        for name in ("report.html", "summary.md", "agent_out.txt"):
            self.assertIn(f"{sp}/{name}", produced)

    def test_script_argv_output(self):
        write_jsonl(self.transcript, [
            tool_use("w1", "Write", {"file_path": "/tmp/s/gen.py",
                                     "content": "import sys\nopen(sys.argv[1], 'w').write('x')\n"}, "1"),
            tool_result("w1", "1b"),
            *self.bash("b1", "python /tmp/s/gen.py /tmp/s/out.csv", "2"),
        ])
        self.assertIn("/tmp/s/out.csv", analyze(self.transcript).produced)

    def test_too_long_command_did_not_run(self):
        write_jsonl(self.transcript, [
            *self.bash("b1", "cat > /tmp/s/big.txt <<'EOF'\nx\nEOF", "1", error="spawn ENAMETOOLONG"),
        ])
        self.assertNotIn("/tmp/s/big.txt", reconstruct(self.transcript))


    def test_first_append_in_own_scratchpad_creates_file(self):
        own = f"/tmp/c/proj/{self.transcript.stem}/scratchpad/log.md"
        other = "/tmp/c/proj/other-session/scratchpad/log.md"
        write_jsonl(self.transcript, [
            *self.bash("b1", f"echo one >> {own} && echo one >> {other}", "1"),
            *self.bash("b2", f"echo two >> {own}", "2"),
        ])
        files = reconstruct(self.transcript)
        self.assertTrue(files[own].complete)
        self.assertEqual(files[own].content, "one\ntwo\n")
        self.assertFalse(files[other].complete)


class ShellAnalysisTests(unittest.TestCase):
    def test_patterns(self):
        fx = analyze_bash(
            "D=/tmp/d; mkdir -p $D\n"
            "cat <<'EOF' | tee \"$D/notes.md\"\nhello\nEOF\n"
            "(echo a; echo b) > $D/group.txt\n"
            "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"\n"
            "python3 -c \"open('$D/x.txt','w')\" && rm -rf $D/old 2>/dev/null",
            "/w",
        )
        self.assertEqual([(w.path, w.content) for w in fx.writes], [("/tmp/d/notes.md", "hello\n")])
        self.assertIn(("/tmp/d/group.txt", "output of a command group"), fx.produced)
        self.assertEqual(fx.deletes, ["/tmp/d/old"])
        self.assertEqual(len(fx.scripts), 1)

    def test_powershell_here_string(self):
        fx = analyze_powershell("Set-Content -Path C:\\Temp\\a.txt -Value @'\nhi\n'@", "C:\\w")
        self.assertEqual([(w.path, w.content) for w in fx.writes], [("C:\\Temp\\a.txt", "hi\n")])


    def test_executed_scripts_flags_and_git_deletes(self):
        fx = analyze_bash("python /tmp/x/gen.py /tmp/x/out.csv && npx shot --output /tmp/x/s.png"
                          " && git worktree remove /tmp/wt && git rm tmp_a.py", "/repo")
        self.assertEqual(fx.executed, [("/tmp/x/gen.py", "py", "/repo", ["/tmp/x/gen.py", "/tmp/x/out.csv"])])
        self.assertEqual([p for p, _ in fx.produced], ["/tmp/x/s.png"])
        self.assertEqual(fx.deletes, ["/tmp/wt", "/repo/tmp_a.py"])

    def test_script_outputs_follow_argv(self):
        code = "import sys\nout = sys.argv[2]\nwith open(out, 'w') as f:\n    f.write('x')\n"
        self.assertEqual(script_outputs(code, "py", ["gen.py", "a", "/tmp/x/b.txt"]), ["/tmp/x/b.txt"])

    def test_powershell_vars_redirects_copies(self):
        fx = analyze_powershell('$d = "C:\\Temp\\w"\npython run.py > $d\\log.txt\n'
                                'Copy-Item a.txt $d\\b.txt\nRemove-Item tmp.ps1', "C:\\repo")
        self.assertEqual(fx.produced, [("C:\\Temp\\w\\log.txt", "output of python")])
        self.assertEqual(fx.copies, [("C:\\repo\\a.txt", "C:\\Temp\\w\\b.txt", False)])
        self.assertEqual(fx.deletes, ["C:\\repo\\tmp.ps1"])


    def test_conditional_writes_are_uncertain(self):
        fx = analyze_bash(
            "if make; then\n  echo ok >> /tmp/log\nelse\n  echo bad >> /tmp/log\nfi\n"
            "for f in a b; do echo x > /tmp/loop; done\n"
            "[ -f x ] && r=yes || r=no\necho \"r=$r\" > /tmp/r\n"
            "S=/tmp; echo fine > $S/sure\n"
            "which rsync && echo a >> /tmp/gated\n"
            "mkdir -p /tmp/d && echo b > /tmp/d/safe\n"
            "make && echo c > /tmp/last",
            "/w",
            succeeded=True,
        )
        unsure = {w.path: w.uncertain for w in fx.writes}
        self.assertEqual(unsure, {"/tmp/log": True, "/tmp/loop": True, "/tmp/r": True, "/tmp/sure": False,
                                  "/tmp/gated": True, "/tmp/d/safe": False, "/tmp/last": False})
        self.assertEqual(fx.produced, [])

    def test_single_quotes_and_command_substitution(self):
        fx = analyze_bash(
            "SP=/tmp/s\n"
            "echo 'cost $SP' > $SP/a\n"
            "echo \"at \\$SP and $SP\" > $SP/b\n"
            "echo \"- step $( [ -d \"$SP/x\" ] && echo worked || echo failed) done\" >> \"$SP/log\"\n",
            "/w",
        )
        writes = {w.path: w for w in fx.writes}
        self.assertEqual(writes["/tmp/s/a"].content, "cost $SP\n")
        self.assertEqual(writes["/tmp/s/b"].content, "at $SP and /tmp/s\n")
        self.assertFalse(writes["/tmp/s/a"].uncertain or writes["/tmp/s/b"].uncertain)
        log = writes["/tmp/s/log"]
        self.assertTrue(log.append and log.uncertain and not log.conditional)
        self.assertEqual(log.parts, ["- step ", None, " done\n"])

    def test_printf_and_echo_escapes(self):
        fx = analyze_bash(
            "printf '%s\\n' \"one\" 'two' > /tmp/p\n"
            "printf 'n=%d%%\\n' 5 2>/dev/null >> /tmp/p\n"
            "printf '%5s' x > /tmp/wide\n"
            "echo -e 'a\\tb' > /tmp/e\n"
            "echo -n 'a\\tb' > /tmp/n\n",
            "/w",
        )
        self.assertEqual([(w.path, w.content) for w in fx.writes], [
            ("/tmp/p", "one\ntwo\n"), ("/tmp/p", "n=5%\n"), ("/tmp/e", "a\tb\n"), ("/tmp/n", "a\\tb"),
        ])
        self.assertIn(("/tmp/wide", "output of printf"), fx.produced)


if __name__ == "__main__":
    unittest.main()
