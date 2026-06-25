from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class DockerAssetsTests(unittest.TestCase):
    def test_compose_runs_stdio_server_with_persistent_cache(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "compose.yaml").read_text()
        readme = (ROOT / "README.md").read_text()
        agents = (ROOT / "AGENTS.md").read_text()
        setup = (ROOT / "scripts" / "setup.sh").read_text()
        setup_scripts = sorted(path.name for path in (ROOT / "scripts").glob("*.sh"))
        setup_libs = sorted(path.name for path in (ROOT / "scripts" / "lib").glob("*.sh"))

        self.assertIn("USER icloud", dockerfile)
        self.assertIn('CMD ["icloud-mcp"]', dockerfile)
        self.assertIn("container_name: icloud-mcp-server", compose)
        self.assertIn('command: ["sleep", "infinity"]', compose)
        self.assertIn("ICLOUD_MCP_DATABASE_PATH: /data/icloud-mcp.sqlite3", compose)
        self.assertIn("ICLOUD_MCP_DASHBOARD_HOST: 0.0.0.0", compose)
        self.assertIn("ICLOUD_MCP_DASHBOARD_PUBLIC_HOST: 127.0.0.1", compose)
        self.assertIn('ICLOUD_MCP_DASHBOARD_ALLOW_EXTERNAL_BIND: "true"', compose)
        self.assertIn('"127.0.0.1:8765-8814:8765-8814"', compose)
        self.assertIn("icloud-mcp-data:/data", compose)
        self.assertIn("./scripts/setup.sh docker", readme)
        self.assertIn('"exec", "-i", "icloud-mcp-server", "icloud-mcp"', readme)
        self.assertNotIn("compose.yaml", readme)
        self.assertIn("Docker: `docker compose up -d`", agents)
        self.assertIn("docker compose build", setup)
        self.assertIn("docker compose up -d", setup)
        self.assertIn('"exec", "-i", "icloud-mcp-server", "icloud-mcp"', setup)
        self.assertNotIn('f"{root}/compose.yaml"', setup)
        self.assertIn("pick_docker_agent", setup)
        self.assertIn("write_docker_client_config", setup)
        self.assertIn("ICLOUD_SETUP_PERSIST_APP_PASSWORD=true", setup)
        self.assertIn("pick_agent", setup)
        self.assertIn("1) printf 'docker\\n'; return ;;", setup)
        self.assertIn("Choice [1-4, default 1]", setup)
        self.assertIn('1) ICLOUD_SETUP_SCOPE="global"; break ;;', setup)
        self.assertIn("Global user config  %s(recommended)%s", setup)
        self.assertNotIn("All MCP clients", setup)
        self.assertNotIn("setup_all()", setup)
        self.assertIn("write_standard_client_config", setup)
        self.assertNotIn("read -r -s", setup)
        self.assertEqual(setup_scripts, ["setup.sh"])
        self.assertEqual(setup_libs, [])
        self.assertNotIn('source "$(cd', setup)
        docker_flow = setup[setup.index("setup_docker()") :]
        self.assertLess(
            docker_flow.index('PYTHON_BIN="$(find_python)"'), docker_flow.index('write_env_file "$env_path"')
        )


if __name__ == "__main__":
    unittest.main()
