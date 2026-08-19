# Blender + MCP — setup state, launch & credentials

Operational doc for the WorldClaw assembly engine. Last verified 2026-08-12.

## Current state (already done)

| Component | Status | Location |
|---|---|---|
| Blender 5.1 | ✅ installed | `C:\Program Files\Blender Foundation\Blender 5.1\blender.exe` |
| blender-mcp (MCP server) | ✅ cached, runs | launched by ZCode via `uvx --python 3.11 blender-mcp` |
| uvx | ✅ | `C:\Users\garyf\AppData\Local\hermes\bin\uvx.exe` |
| Blender addon "Blender MCP" | ✅ installed + enabled + saved to user prefs | `…\Blender Foundation\Blender\5.1\scripts\addons\addon.py` |
| addon.py (source copy) | ✅ | `C:\ClaudeProjects\tools\blender-mcp-addon\addon.py` |
| install script | ✅ | `C:\ClaudeProjects\tools\blender-mcp-addon\install_blender_mcp.py` |
| ZCode MCP config | ✅ created | `C:\Users\garyf\.zcode\cli\config.json` → `mcp.servers.blender` |

Config registered (full uvx path — GUI apps can't rely on PATH):
```json
{
  "mcp": {
    "servers": {
      "blender": {
        "command": "C:\\Users\\garyf\\AppData\\Local\\hermes\\bin\\uvx.exe",
        "args": ["--python", "3.11", "blender-mcp"],
        "env": { "UV_PYTHON_PREFERENCE": "only-managed" }
      }
    }
  }
}
```

## What YOU must do to go live (one-time, ~2 min)

1. **Open Blender 5.1** (GUI).
2. `Edit > Preferences > Add-ons` → confirm **"Blender MCP"** is checked (it should be — we enabled it). If not, click it on.
3. In the 3D viewport, press **N** → open the **BlenderMCP** tab → port should be **9876** → click **"Connect to Claude"**. The addon now listens on `localhost:9876`.
4. **Restart ZCode** so it loads the `blender` MCP server from the new config. After restart, `mcp__blender__*` tools should appear (e.g. `get_scene_info`, `create_cube`, `set_material`, `execute_code`).

Keep Blender open + Connected whenever you want the agent to drive it.

## (Optional) Wire Hunyuan3D + Sketchfab into the bridge

blender-mcp can call Hunyuan3D and Sketchfab directly from inside Blender. To enable,
add these to the server's `env` block in `C:\Users\garyf\.zcode\cli\config.json` (then
restart ZCode). **Fill the values yourself** — the agent intentionally does not handle
your secret values:

```json
"env": {
  "UV_PYTHON_PREFERENCE": "only-managed",
  "BLENDERMCP_HUNYUAN3D_SECRET_ID": "<same as your TENCENTCLOUD_SECRET_ID>",
  "BLENDERMCP_HUNYUAN3D_SECRET_KEY": "<same as your TENCENTCLOUD_SECRET_KEY>",
  "BLENDERMCP_HUNYUAN3D_API_URL": "https://hunyuan3d.tencentcsapi.com",
  "BLENDERMCP_SKETCHFAB_API_KEY": "<optional, for library scatter assets>"
}
```

The Hunyuan3D creds are the same `TENCENTCLOUD_SECRET_ID/SECRET_KEY` the
`hunyuan3d-pipeline` skill uses (TC3-HMAC signing) — the addon has the signing code
built in.

## Verification checklist

- [ ] `python -m py_compile` on addon.py → OK (done)
- [ ] addon installed + module `addon` enabled + userpref saved (done)
- [ ] `uvx --python 3.11 blender-mcp` launches and logs "BlenderMCP server starting
      up" (done — it then waits for a client on stdin, which is correct)
- [ ] ZCode config JSON valid, `mcp.servers.blender` present (done)
- [ ] **(you)** Blender open → BlenderMCP tab → Connect → no error
- [ ] **(you)** ZCode restart → `mcp__blender__get_scene_info` returns the scene
- [ ] **(you)** smoke test: create a cube via the MCP tool, see it in the viewport

## Troubleshooting

- **MCP server not connecting in ZCode** → use Settings → MCP to inspect status; if the
  full uvx path is wrong, update `command` in the config. Load the `diagnosing-mcp`
  skill for the symptom→fix flow.
- **"Failed to connect to Blender" / WinError 10061** → the Blender addon's socket
  server isn't running. Open Blender, N panel → BlenderMCP → Connect first.
- **Addon won't enable on Blender 5.1** (bleeding-edge) → it's a legacy addon; if 5.1's
  extensions system rejects it, reinstall via the GUI Install dialog, or fall back to
  driving Blender with `blender --background --python` scripts (no addon needed for
  pure bpy work — only the live socket bridge needs the addon).
- **Port conflict on 9876** → change `BLENDER_PORT` in the BlenderMCP panel and match it
  in the addon; default is fine for single-user.
