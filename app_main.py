"""Entry point for the frozen step2kit.exe: a proper desktop window, not a browser tab.

A frozen build has no standalone python.exe to spawn a worker with, so the exe re-invokes itself:
server.py's run_job() launches `step2kit.exe --worker <file> ...` as the isolated job subprocess (see
server.py's FROZEN branch of cfg_to_argv), and this dispatches that straight into the CLI instead of
starting a second server/window.

Otherwise, the local HTTP server (server.py, unchanged) starts in a background thread exactly as before,
and a native window (pywebview, backed by the Windows WebView2 runtime that ships with Win10/11) points
at it -- same UI, same endpoints, but presented as its own window with its own icon and taskbar entry
instead of a tab in whatever browser happens to be default.
"""
import sys

HOST, PORT = "127.0.0.1", 4790

if len(sys.argv) > 1 and sys.argv[1] == "--worker":
    from step2kit.cli import main as cli_main
    sys.exit(cli_main(sys.argv[2:]))
else:
    import server
    server.start_background(HOST, PORT)

    try:
        import webview
        webview.settings["ALLOW_DOWNLOADS"] = True   # off by default; needed for the DXF/STEP/report links
        webview.create_window("step2kit", f"http://{HOST}:{PORT}/", width=1280, height=880, min_size=(760, 560))
        webview.start()
    except Exception:
        # WebView2 runtime missing or otherwise unavailable: degrade to a browser tab rather than a
        # silent crash, since the server itself is already up and fully functional either way.
        import traceback
        import webbrowser
        traceback.print_exc()
        print("Falling back to opening a browser tab instead.")
        webbrowser.open(f"http://{HOST}:{PORT}/")
        input("Press Enter to quit step2kit...\n")
