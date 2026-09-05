"""Request trusted completion validation using native workflow_dispatch."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from c6_auto_resume import AUTO_ANCHOR, REPOSITORY, GitHub, decode, require


def main():
    require(os.environ["GITHUB_REPOSITORY"] == REPOSITORY
            and os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
            and os.environ["GITHUB_RUN_ATTEMPT"] == "1", "invalid completion request")
    event = decode(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    require(event["inputs"]["workflow_revision"] == os.environ["GITHUB_SHA"]
            and os.environ["GITHUB_REF_NAME"] == AUTO_ANCHOR, "untrusted handoff revision")
    github = GitHub()
    anchor = github.read("git/ref/heads/" + AUTO_ANCHOR)
    require(anchor["object"]["sha"] == os.environ["GITHUB_SHA"], "workflow anchor moved")
    github.read("actions/workflows/c6-auto-resume.yml/dispatches", payload={
        "ref": AUTO_ANCHOR, "inputs": {"predecessor_run_id": os.environ["GITHUB_RUN_ID"]}})
    print("Requested authenticated completion validation for", os.environ["GITHUB_RUN_ID"])


if __name__ == "__main__":
    main()
