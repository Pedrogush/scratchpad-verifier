import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scratchpad_verifier.cli import main
from scratchpad_verifier.transcript import reconstruct


def tool_use(tid, name, inp, ts):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp},
        ]},
    }


def tool_result(tid, ts, result=None, is_error=False):
    rec = {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "ok", "is_error": is_error},
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

    def test_extract_and_verify(self):
        out_dir = self.dir / "out"
        rc, _ = self.run_cli("extract", str(self.transcript), "-o", str(out_dir))
        self.assertEqual(rc, 0)
        self.assertEqual((out_dir / SP.lstrip("/")).read_text(), "hello\n")

        rc, out = self.run_cli("verify", str(self.transcript), "--root", str(out_dir))
        self.assertEqual(rc, 0)
        self.assertIn("MATCH", out)

        (out_dir / SP.lstrip("/")).write_text("changed\n")
        rc, out = self.run_cli("verify", str(self.transcript), "--root", str(out_dir))
        self.assertEqual(rc, 1)
        self.assertIn("DIFFERS", out)

    def test_list_json(self):
        rc, out = self.run_cli("list", str(self.transcript), "--json")
        data = json.loads(out)
        self.assertEqual(data[0]["path"], SP)
        self.assertEqual(data[0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
