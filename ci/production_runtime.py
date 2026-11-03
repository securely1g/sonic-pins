#!/usr/bin/env python3
"""Build production TARs and run all P4RT tests through the existing Common SDK."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

from runtime_contract import PROFILES, seal_contracts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buildimage", required=True, type=Path)
    parser.add_argument("--sdk", required=True, type=Path)
    parser.add_argument("--output-base", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]

    def identity():
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True).strip():
            raise ValueError("Production acceptance requires a clean source checkout")
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()

    source_commit = identity()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    sdk = args.sdk.resolve()
    # Reuse the established SDK/no-DEB auditor; the workflow pins this checkout.
    spec = importlib.util.spec_from_file_location(
        "sdk_acceptance", args.buildimage.resolve() / "tools/bazel/ci/p4rt_common_sdk.py")
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)
    env = dict(os.environ, USE_BAZEL_VERSION="6.4.0")
    bazel = ["bazel", "--output_base=" + str(args.output_base.resolve())]
    override = "--override_repository=sonic_swss_common=" + str(sdk)
    options = ["--config=p4rt-runtime", override, "--jobs=" + str(args.jobs)]
    packages = ["//p4rt_app:" + name for name in PROFILES]
    templates = [target + "_contract_template" for target in packages]
    private = args.output_base.resolve().parent / "p4rt-runtime-private"
    private.mkdir(parents=True, exist_ok=True)

    def run(arguments, **kwargs):
        return subprocess.run([*bazel, *arguments], cwd=source, env=env,
                              check=True, **kwargs)

    def capture(arguments):
        return run(arguments, text=True, stdout=subprocess.PIPE).stdout.strip()

    def record(name, value):
        (output / name).write_text(json.dumps(value, indent=2) + "\n")

    try:
        version = capture(["version", "--gnu_format"])
        if [line for line in version.splitlines() if line.startswith("bazel ")] != ["bazel 6.4.0"]:
            raise ValueError("The production profile requires Bazel 6.4.0")
        tests = sorted(capture(["query", override, "tests(//p4rt_app/...)",
                                "--output=label"]).splitlines())
        if ("//p4rt_app:runtime_package_test" not in tests or
                any(not label.startswith(("//p4rt_app/", "//p4rt_app:")) for label in tests)):
            raise ValueError("Expected all original P4RT tests and the installed-package test")
        (output / "test-targets.txt").write_text("\n".join(tests) + "\n")
        graph_path = private / "actions.json"
        with graph_path.open("w") as stream:
            run(["aquery", *options, "--output=jsonproto", "--include_artifacts",
                 "--include_commandline=false", "deps(set(" + " ".join(packages + templates + tests) + "))"],
                stdout=stream)
        record("action-audit.json", acceptance.audit_graph(json.loads(graph_path.read_text())))
        graph_path.unlink()
        run(["build", *options, *packages, *templates])
        execution_root = Path(capture(["info", *options, "execution_root"]))
        for target in packages + templates:
            files = capture(["cquery", *options, target, "--output=files"]).splitlines()
            suffix = ".tar" if target in packages else ".json"
            if len(files) != 1 or not files[0].endswith(suffix):
                raise ValueError("Unexpected production artifact: " + target)
            shutil.copyfile(execution_root / files[0], output / Path(files[0]).name)
        events = private / "test-events.json"
        result = subprocess.run([*bazel, "test", *options, "--nocache_test_results",
                                 "--test_output=errors", "--nozip_undeclared_test_outputs",
                                 "--build_event_json_file=" + str(events),
                                 *tests], cwd=source, env=env)
        statuses = {}
        if events.exists():
            for line in events.read_text().splitlines():
                event = json.loads(line)
                if "testSummary" in event:
                    statuses[event["id"]["testSummary"]["label"]] = event["testSummary"]["overallStatus"]
        record("test-results.json", statuses)
        result.check_returncode()
        if set(statuses) != set(tests) or any(status != "PASSED" for status in statuses.values()):
            raise ValueError("Every original and installed P4RT test must execute and pass")
        testlogs = Path(capture(["info", *options, "bazel-testlogs"]))
        installed = testlogs / "p4rt_app/runtime_package_test/test.outputs/installed-packages.json"
        shutil.copyfile(installed, output / "installed-packages.json")
        for label in tests:
            relative = label.removeprefix("//").replace(":", "/")
            destination = output / "test-results" / relative / "test.xml"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(testlogs / relative / "test.xml", destination)
        shutil.copyfile(sdk / "sdk-build.json", output / "sdk-build.json")
        if identity() != source_commit:
            raise ValueError("Source changed during production acceptance")
        contracts = seal_contracts(output, source, source_commit)
        record("source.json", {"commit": source_commit,
            "bazel": "6.4.0", "options": options, "architecture": "amd64",
            "tar_sha256": {item.name: acceptance.digest(item) for item in output.glob("*.tar")},
            "contract_sha256": {item.name: acceptance.digest(item) for item in contracts.values()}})
        print(f"PASS: two production TARs and all {len(tests)} P4RT tests")
    finally:
        subprocess.run([*bazel, "shutdown"], cwd=source, env=env, check=False)


if __name__ == "__main__":
    main()
