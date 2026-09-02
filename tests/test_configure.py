import plistlib
import unittest
from pathlib import Path

import configure


class LaunchdAgentTests(unittest.TestCase):
    def test_specs_use_runtime_paths_instead_of_committed_user_paths(self) -> None:
        working_directory = Path("/Users/tester/Developer/beeper-poke-bridge")
        home = Path("/Users/tester")
        runner = [
            "/opt/homebrew/bin/uv",
            "run",
            "--with-requirements",
            "requirements.txt",
            "python",
        ]

        specs = dict(
            configure._launchd_agent_specs(
                runner,
                "/opt/homebrew/bin/npx",
                working_directory,
                home,
            )
        )

        self.assertEqual(
            specs["co.eightstate.poke-bridge"]["ProgramArguments"],
            runner + [str(working_directory / "bridge.py")],
        )
        self.assertEqual(
            specs["co.eightstate.poke-tunnel"]["ProgramArguments"][0],
            "/opt/homebrew/bin/npx",
        )
        for data in specs.values():
            self.assertEqual(data["WorkingDirectory"], str(working_directory))
            self.assertEqual(data["EnvironmentVariables"]["HOME"], str(home))
            self.assertNotIn("/Users/alex", plistlib.dumps(data).decode("utf-8"))

    def test_tunnel_agent_is_optional_when_npx_is_missing(self) -> None:
        specs = configure._launchd_agent_specs(
            ["/usr/local/bin/python3"],
            None,
            Path("/tmp/beeper-poke-bridge"),
            Path("/Users/tester"),
        )

        self.assertEqual([label for label, _ in specs], ["co.eightstate.poke-bridge"])

    def test_generated_agents_round_trip_as_xml_plists(self) -> None:
        for _, data in configure._launchd_agent_specs(
            ["/usr/local/bin/python3"],
            "/usr/local/bin/npx",
            Path("/tmp/beeper-poke-bridge"),
            Path("/Users/tester"),
        ):
            encoded = plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=False)
            self.assertEqual(plistlib.loads(encoded), data)


if __name__ == "__main__":
    unittest.main()
