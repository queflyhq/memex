"""Cross-platform service installer.

The pitch: memex must be running for AI editors to use it. Asking users
to run `memex daemon` every boot is a non-starter — they'll forget,
their AI will go to an empty graph, they'll churn.

This module wires `memex` to start on login automatically:

  - macOS  : LaunchAgent at ~/Library/LaunchAgents/com.quefly.memex.plist
  - Windows: scheduled task `Memex Daemon` running at logon (Task Scheduler)
  - Linux  : systemd user unit at ~/.config/systemd/user/memex.service

All paths user-scope (no admin/root needed). All idempotent — re-running
`memex service install` overwrites cleanly. Uninstall removes the
config and stops the running service.

CLI: `memex service install` / `memex service uninstall` / `memex service status`.
"""

from memex.service.installer import (
    install_service,
    uninstall_service,
    service_status,
)

__all__ = ["install_service", "uninstall_service", "service_status"]
