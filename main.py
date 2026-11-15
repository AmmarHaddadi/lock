import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --- Configuration ---
# You can paste your Discord webhook URL here directly, or supply it via
# the DISCORD_WEBHOOK_URL environment variable or the --webhook / -w CLI argument.
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1571206978968101034/yYgQvopWxu6V5PWRZZFaAxBX_SrqFpTdszIWAy4lMySlh9BO9gD2pPK86G5zP54E4wd8"

# Trigger file path for remote lock signaling
TRIGGER_FILE = Path("/tmp") / f"lock_screen_trigger_{os.getuid()}"


def get_webhook_url(arg_url: str | None = None) -> str:
    """Resolves Discord webhook URL with precedence: CLI arg > Env var > Code default."""
    if arg_url and arg_url.strip():
        return arg_url.strip()
    env_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if env_url:
        return env_url
    return DISCORD_WEBHOOK_URL.strip()


def format_duration(seconds: float) -> str:
    """Formats seconds into human-readable duration string (e.g. 1h 24m 12s)."""
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def send_discord_notification(
    webhook_url: str | None,
    title: str,
    description: str,
    fields: list[tuple[str, str]] | None = None,
    color: int = 0x5865F2,
) -> None:
    """Sends a formatted embed message to a Discord webhook."""
    if not webhook_url:
        return

    payload = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "fields": [
                    {"name": name, "value": str(val), "inline": True}
                    for name, val in (fields or [])
                ],
                "footer": {"text": "Workstation Screen Guard"},
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ]
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "WorkstationScreenGuard/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status in (200, 204):
                print("[Discord] Notification delivered.")
    except Exception as e:
        print(f"[Discord] Warning: Failed to deliver notification: {e}")


def lock_screen() -> bool:
    """Locks the workstation screen without requiring root access.

    Works on LightDM / GNOME setups (even where standard gnome-screensaver
    or loginctl fail due to missing GDM integration) and handles remote SSH
    sessions by ensuring XDG_SEAT_PATH is set.
    """
    print("\nInitiating screen lock...")

    env = os.environ.copy()
    if "XDG_SEAT_PATH" not in env:
        # Default to Seat0 if running from a remote SSH shell
        env["XDG_SEAT_PATH"] = "/org/freedesktop/DisplayManager/Seat0"

    # Method 1: LightDM's dm-tool (recommended for this environment)
    if shutil.which("dm-tool"):
        try:
            res = subprocess.run(
                ["dm-tool", "lock"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                print("Screen successfully locked via dm-tool.")
                return True
        except Exception as e:
            print(f"dm-tool attempt failed: {e}")

    # Method 2: Direct D-Bus call to DisplayManager Seat0
    try:
        res = subprocess.run(
            [
                "dbus-send",
                "--system",
                "--dest=org.freedesktop.DisplayManager",
                "/org/freedesktop/DisplayManager/Seat0",
                "org.freedesktop.DisplayManager.Seat.Lock",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            print("Screen successfully locked via D-Bus DisplayManager.")
            return True
    except Exception as e:
        print(f"D-Bus attempt failed: {e}")

    # Method 3: Fallback loginctl
    if shutil.which("loginctl"):
        try:
            res = subprocess.run(
                ["loginctl", "lock-session"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                print("Screen lock command sent via loginctl.")
                return True
        except Exception as e:
            print(f"loginctl attempt failed: {e}")

    print("Error: Could not lock the screen.")
    return False


def test_lock(countdown_seconds: int = 3, webhook_url: str | None = None) -> None:
    """Provides a visible countdown before locking so the user can test the mechanism."""
    print(f"Testing screen lock! Locking screen in {countdown_seconds} seconds...")
    start_time = time.time()
    for i in range(countdown_seconds, 0, -1):
        print(f"  {i}...")
        time.sleep(1)
    if lock_screen():
        elapsed = time.time() - start_time
        send_discord_notification(
            webhook_url,
            title="🔒 Test Screen Lock Triggered",
            description="The test screen lock was executed successfully.",
            fields=[
                ("Host", socket.gethostname()),
                ("Elapsed", format_duration(elapsed)),
            ],
            color=0xF1C40F,
        )


def run_trap(
    arm_delay: float = 2.0,
    keep_awake: bool = True,
    minutes: float | None = None,
    webhook_url: str | None = None,
) -> None:
    """Displays a fullscreen black window that locks the screen upon detecting

    any mouse or keyboard input, or automatically when the timer expires.
    """
    import tkinter as tk
    from pynput import keyboard, mouse

    start_time = time.time()
    hostname = socket.gethostname()
    username = os.environ.get("USER", "unknown")

    print("Initializing black trap screen...")
    print(f"Arming delay: {arm_delay:.1f}s (hands off mouse & keyboard)")
    print(
        f"Timer: {f'{minutes:.1f} minutes auto-lock' if minutes is not None else 'Indefinite (until input or remote lock)'}"
    )
    print(f"Keep-awake jiggler: {'Enabled' if keep_awake else 'Disabled'}")
    print(f"Discord notifications: {'Enabled' if webhook_url else 'Disabled'}")
    print(f"Remote lock trigger file: {TRIGGER_FILE}\n")

    # Send startup Discord notification
    if webhook_url:
        send_discord_notification(
            webhook_url,
            title="🛡️ Trap Mode Activated",
            description="Workstation is guarded by a fullscreen black trap screen.",
            fields=[
                ("Host", hostname),
                ("User", username),
                ("Timer", f"{minutes:.1f} mins" if minutes else "Indefinite"),
                ("Keep-Awake", "Enabled" if keep_awake else "Disabled"),
            ],
            color=0x3498DB,
        )

    # Clean up any stale trigger file
    if TRIGGER_FILE.exists():
        try:
            TRIGGER_FILE.unlink()
        except OSError:
            pass

    root = tk.Tk()
    root.title("ScreenLockTrap")
    root.configure(bg="black", cursor="none")
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)

    mouse_ctrl = mouse.Controller()

    end_time = start_time + (minutes * 60) if minutes is not None else None

    armed = False
    triggered = False
    is_jiggling = False
    last_mouse_pos = mouse_ctrl.position

    def on_trigger(reason: str):
        nonlocal triggered
        if triggered:
            return
        triggered = True
        print(f"\n[TRAP TRIGGERED] {reason}")

        # Stop input listeners immediately to prevent further triggers
        try:
            m_listener.stop()
            k_listener.stop()
        except Exception:
            pass

        def execute_lock_and_close():
            # 1. Lock screen FIRST while the black window covers the display
            locked = lock_screen()
            total_spent = time.time() - start_time
            formatted_time = format_duration(total_spent)
            print(f"Total time spent before lock: {formatted_time}")

            # Send Discord notification after lock
            if webhook_url and locked:
                send_discord_notification(
                    webhook_url,
                    title="🔒 Workstation Locked",
                    description="The screen was locked successfully.",
                    fields=[
                        ("Trigger Reason", reason),
                        ("Total Active Time", formatted_time),
                        ("Host", hostname),
                        ("User", username),
                    ],
                    color=0xE74C3C,
                )

            # 2. Wait 5 seconds, then close the black window
            print("Holding black window for 5 seconds before closing...")
            root.after(5000, root.destroy)

        root.after(0, execute_lock_and_close)

    def arm_trap():
        nonlocal armed, last_mouse_pos
        last_mouse_pos = mouse_ctrl.position
        armed = True
        print(">>> [TRAP ARMED] Any mouse or keyboard activity will immediately lock the screen.")

    # Tkinter event bindings
    def on_tk_key(event):
        if armed:
            on_trigger(f"Key press detected ({event.keysym})")

    def on_tk_motion(event):
        nonlocal last_mouse_pos
        if not armed or is_jiggling:
            last_mouse_pos = (event.x, event.y)
            return
        dx = abs(event.x - last_mouse_pos[0])
        dy = abs(event.y - last_mouse_pos[1])
        if dx > 3 or dy > 3:
            on_trigger(f"Mouse movement detected ({dx}, {dy}px)")

    def on_tk_button(event):
        if armed:
            on_trigger(f"Mouse button click detected ({event.num})")

    root.bind("<Key>", on_tk_key)
    root.bind("<Motion>", on_tk_motion)
    root.bind("<Button>", on_tk_button)
    root.bind("<MouseWheel>", lambda e: on_trigger("Mouse scroll detected") if armed else None)

    # Global OS-level listeners via pynput to catch events even if window loses focus
    def pynput_on_move(x, y):
        nonlocal last_mouse_pos
        if not armed or is_jiggling:
            last_mouse_pos = (x, y)
            return
        dx = abs(x - last_mouse_pos[0])
        dy = abs(y - last_mouse_pos[1])
        if dx > 3 or dy > 3:
            on_trigger(f"Global mouse movement detected ({dx}, {dy}px)")

    def pynput_on_click(x, y, button, pressed):
        if armed and pressed:
            on_trigger(f"Global mouse click detected ({button})")

    def pynput_on_scroll(x, y, dx, dy):
        if armed:
            on_trigger("Global mouse scroll detected")

    def pynput_on_press(key):
        if armed:
            on_trigger(f"Global key press detected ({key})")

    m_listener = mouse.Listener(
        on_move=pynput_on_move,
        on_click=pynput_on_click,
        on_scroll=pynput_on_scroll,
    )
    k_listener = keyboard.Listener(on_press=pynput_on_press)
    m_listener.start()
    k_listener.start()

    # Periodic keep-awake jiggle (moves by 1px and back without triggering the trap)
    def do_keep_awake():
        nonlocal is_jiggling, last_mouse_pos
        if not triggered and armed and keep_awake:
            is_jiggling = True
            try:
                x, y = mouse_ctrl.position
                mouse_ctrl.position = (x + 1, y)
                time.sleep(0.05)
                mouse_ctrl.position = (x, y)
                last_mouse_pos = (x, y)
            except Exception:
                pass
            finally:
                is_jiggling = False
            root.after(30000, do_keep_awake)

    # Periodic checks for remote trigger file and timer expiration
    def periodic_check():
        if TRIGGER_FILE.exists():
            try:
                TRIGGER_FILE.unlink()
            except OSError:
                pass
            on_trigger("Remote signal received")
            return

        if end_time is not None and time.time() >= end_time:
            on_trigger(f"Timer finished ({minutes:.1f} minutes elapsed). Auto-locking screen.")
            return

        if not triggered:
            root.after(500, periodic_check)

    # Signal handling for Ctrl+C in terminal
    def handle_signal(sig, frame):
        print("\nTrap stopped manually.")
        try:
            root.destroy()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Schedule arming and background checks
    root.after(int(arm_delay * 1000), arm_trap)
    if keep_awake:
        root.after(30000, do_keep_awake)
    root.after(500, periodic_check)

    # Run GUI event loop
    try:
        root.mainloop()
    finally:
        m_listener.stop()
        k_listener.stop()


def run_jiggler(
    minutes: float,
    lock_on_finish: bool = True,
    webhook_url: str | None = None,
) -> None:
    from pynput.mouse import Controller

    start_time = time.time()
    hostname = socket.gethostname()
    username = os.environ.get("USER", "unknown")

    mouse = Controller()
    print(f"Mouse jiggler running for {minutes:.1f} minutes.")
    print(f"Auto-lock on completion: {'Enabled' if lock_on_finish else 'Disabled'}")
    print(f"Discord notifications: {'Enabled' if webhook_url else 'Disabled'}")
    print(f"Remote lock trigger file: {TRIGGER_FILE}")
    print("Press Ctrl+C to stop manually.\n")

    if webhook_url:
        send_discord_notification(
            webhook_url,
            title="🐭 Mouse Jiggler Started",
            description=f"Preventing screen lock for {minutes:.1f} minutes.",
            fields=[
                ("Host", hostname),
                ("User", username),
                ("Duration", f"{minutes:.1f} mins"),
                ("Auto-Lock", "Enabled" if lock_on_finish else "Disabled"),
            ],
            color=0x2ECC71,
        )

    if TRIGGER_FILE.exists():
        try:
            TRIGGER_FILE.unlink()
        except OSError:
            pass

    interrupted = False

    def handle_signal(sig, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, handle_signal)

    end_time = start_time + (minutes * 60)

    try:
        while time.time() < end_time and not interrupted:
            if TRIGGER_FILE.exists():
                print("\n[Trigger] Remote lock signal received!")
                try:
                    TRIGGER_FILE.unlink()
                except OSError:
                    pass
                locked = lock_screen()
                elapsed = time.time() - start_time
                if webhook_url and locked:
                    send_discord_notification(
                        webhook_url,
                        title="🔒 Workstation Locked (Remote Trigger)",
                        description="Screen locked via remote trigger signal.",
                        fields=[
                            ("Total Active Time", format_duration(elapsed)),
                            ("Host", hostname),
                            ("User", username),
                        ],
                        color=0xE74C3C,
                    )
                return

            x, y = mouse.position
            mouse.position = (x + 1, y)
            time.sleep(0.1)
            mouse.position = (x, y)

            remaining_sleep = 10.0
            while remaining_sleep > 0 and time.time() < end_time and not interrupted:
                if TRIGGER_FILE.exists():
                    break
                step = min(remaining_sleep, 0.5)
                time.sleep(step)
                remaining_sleep -= step

        if interrupted:
            print("\nStopped manually.")
            return

        print("\nTimer finished. Stopping mouse jiggler.")
        if lock_on_finish:
            locked = lock_screen()
            elapsed = time.time() - start_time
            if webhook_url and locked:
                send_discord_notification(
                    webhook_url,
                    title="🔒 Workstation Locked (Timer Completed)",
                    description="Mouse jiggler timer finished and locked the screen.",
                    fields=[
                        ("Total Active Time", format_duration(elapsed)),
                        ("Host", hostname),
                        ("User", username),
                    ],
                    color=0xE74C3C,
                )

    except Exception as e:
        print(f"\nError in jiggler loop: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mouse jiggler, fullscreen trap window, and instant screen lock without root.",
        epilog="""examples:
  uv run python main.py -b -m 120           # Black trap screen for 120m, then auto-lock (or locks earlier on input)
  uv run python main.py --trap              # Black trap screen indefinitely until any input
  uv run python main.py --test              # Test lock with a 3-second countdown
  uv run python main.py --lock              # Lock the screen immediately (0s delay)
  uv run python main.py -m 60               # Mouse jiggler only for 60 mins, then lock
  uv run python main.py -b -w "https://discord.com/api/webhooks/..." # With Discord webhook
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-b",
        "--trap",
        "--blank",
        dest="trap",
        action="store_true",
        help="Show fullscreen black window that locks immediately on any mouse or keyboard input (can be combined with -m)",
    )
    parser.add_argument(
        "-t",
        "--test",
        action="store_true",
        help="Test screen lock after a 3-second countdown",
    )
    parser.add_argument(
        "-l",
        "--lock",
        action="store_true",
        help="Lock the screen immediately and exit (useful for remote locking via SSH)",
    )
    parser.add_argument(
        "-m",
        "--minutes",
        type=float,
        help="How many minutes to run before automatically stopping and locking",
    )
    parser.add_argument(
        "-w",
        "--webhook",
        type=str,
        help="Discord webhook URL to post start and lock notifications (also checks DISCORD_WEBHOOK_URL env / in-code var)",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Do not lock the screen when the timer finishes (for -m jiggler mode)",
    )
    parser.add_argument(
        "--no-jiggle",
        action="store_true",
        help="Disable periodic wake-up jiggle during trap mode",
    )
    parser.add_argument(
        "--remote-lock",
        action="store_true",
        help="Signal a running background instance to lock immediately, or lock directly if none is running",
    )

    # Default behavior when run with no arguments: show the help menu
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()
    webhook_url = get_webhook_url(args.webhook)

    if args.trap:
        run_trap(
            arm_delay=2.0,
            keep_awake=not args.no_jiggle,
            minutes=args.minutes,
            webhook_url=webhook_url,
        )
        return

    if args.test:
        test_lock(webhook_url=webhook_url)
        return

    if args.lock:
        start_time = time.time()
        if lock_screen() and webhook_url:
            send_discord_notification(
                webhook_url,
                title="🔒 Immediate Screen Lock",
                description="Workstation screen locked immediately via CLI.",
                fields=[("Host", socket.gethostname()), ("User", os.environ.get("USER", "unknown"))],
                color=0xE74C3C,
            )
        return

    if args.remote_lock:
        TRIGGER_FILE.touch()
        print(f"Trigger file created at {TRIGGER_FILE}.")
        lock_screen()
        return

    if args.minutes is not None:
        run_jiggler(
            minutes=args.minutes,
            lock_on_finish=not args.no_lock,
            webhook_url=webhook_url,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
