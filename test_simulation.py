import time
import threading
from core.facade import IntellicutFacade
from ui.console_ui import ConsoleUI


def main():
    print("Intellicut MVP - Emulation Mode Test")

    system = IntellicutFacade()
    ui = ConsoleUI()

    # Принудительно создаем источники без камер
    system.ingest.emulation_mode = True
    system.setup_scene(["Virtual Cam 1", "Virtual Cam 2", "Virtual Cam 3"])

    stop_event = threading.Event()

    try:
        system.start()

        def run_loop():
            while not stop_event.is_set():
                system.tick()
                ui.render_preview(system.ingest.get_sources(), system.ingest.emulation_mode)
                time.sleep(0.5)

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()

        print("Running emulation. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nStopping...")
        stop_event.set()
        system.stop()
        system.ingest.stop_all()


if __name__ == "__main__":
    main()
