import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from agents.platform.scripts.verify_skills_provenance import (
    verify_provenance,
    compute_sha256,
)


class TestVerifySkillsProvenance(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.skills_dir = os.path.join(self.test_dir.name, "skills")
        os.makedirs(self.skills_dir)

        # Create two sample skills
        self.file1 = os.path.join(self.skills_dir, "SKILL.md")
        self.file2 = os.path.join(self.skills_dir, "script.py")
        with open(self.file1, "w", encoding="utf-8") as f:
            f.write("# Skill 1\n")
        with open(self.file2, "w", encoding="utf-8") as f:
            f.write("print('hello')\n")

        # Create manifest
        self.manifest_path = os.path.join(self.test_dir.name, "skills_manifest.sha256")
        self._write_manifest()

    def tearDown(self):
        self.test_dir.cleanup()

    def _write_manifest(self):
        h1 = compute_sha256(self.file1)
        h2 = compute_sha256(self.file2)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            f.write(f"{h1}  ./SKILL.md\n")
            f.write(f"{h2}  ./script.py\n")

    def test_valid_manifest(self):
        """Valid manifest with unchanged files should succeed."""
        self.assertTrue(verify_provenance(self.manifest_path, self.skills_dir))

    def test_corrupted_hash(self):
        """Mutating a file's contents should fail verification."""
        with open(self.file1, "w", encoding="utf-8") as f:
            f.write("# Corrupted content\n")
        with self.assertRaises(RuntimeError, msg="Should raise on corrupted hash") as cm:
            verify_provenance(self.manifest_path, self.skills_dir)
        self.assertIn("Checksum mismatch", str(cm.exception))

    def test_missing_file(self):
        """Deleting a tracked file should fail verification."""
        os.remove(self.file2)
        with self.assertRaises(RuntimeError, msg="Should raise on missing file") as cm:
            verify_provenance(self.manifest_path, self.skills_dir)
        self.assertIn("Manifest file missing from directory", str(cm.exception))

    def test_untracked_injected_script(self):
        """Adding an untracked script should fail verification."""
        injected = os.path.join(self.skills_dir, "backdoor.py")
        with open(injected, "w", encoding="utf-8") as f:
            f.write("import os; os.system('echo pwned')\n")
        with self.assertRaises(RuntimeError, msg="Should raise on untracked file") as cm:
            verify_provenance(self.manifest_path, self.skills_dir)
        self.assertIn("Untracked or unauthorized file detected", str(cm.exception))

    def test_missing_manifest(self):
        """Non-existent manifest path should raise RuntimeError."""
        with self.assertRaises(RuntimeError):
            verify_provenance("/non/existent/manifest.sha256", self.skills_dir)

    def test_profile_pvc_copy_verification(self):
        """Verifying a runtime PVC copy of skills against an immutable template manifest should succeed and catch mutations."""
        pvc_dir = os.path.join(self.test_dir.name, "pvc_skills")
        shutil.copytree(self.skills_dir, pvc_dir)
        self.assertTrue(verify_provenance(self.manifest_path, pvc_dir))

        with open(os.path.join(pvc_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("# Corrupted PVC content\n")
        with self.assertRaises(RuntimeError) as cm:
            verify_provenance(self.manifest_path, pvc_dir)
        self.assertIn("Checksum mismatch", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
