import time
import atexit
from core.facade import IntellicutFacade
from ui.console_ui import ConsoleUI


def main():
    print("Intellicut MVP v1.0 (Real Video) Starting...")

    system = IntellicutFacade()
    ui = ConsoleUI()

    # Настройка: подключаем реальные камеры (ID 0, 1, 2)
    # Если камер нет, будут ошибки при старте Ingest
    try:
        system.setup_scene(["Camera 1", "Camera 2", "Camera 3"], reset=True)
        # Для теста пока оставляем эмуляцию в facade,
        # но ingest уже готов к работе с cv2
    except Exception as e:
        print(f"Warning: {e}")

    cleanup_done = False
    started = False

    def _cleanup():
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        try:
            ui.close_windows()
        except Exception:
            pass
        try:
            system.stop()
        except Exception:
            pass
        try:
            system.ingest.stop_all()
        except Exception:
            pass

    atexit.register(_cleanup)

    try:
        system.start()
        started = True
        print("System running. Press Q in video window or Ctrl+C to stop.")
        next_preview_ts = 0.0
        while True:
            system.tick()
            sources = system.ingest.get_sources()
            now = time.time()

            if now >= next_preview_ts:
                ui.render_preview(sources, system.ingest.emulation_mode)
                next_preview_ts = now + 1.0

            should_continue, output_frame = ui.render_selected_camera(
                system.ingest,
                sources,
                system.switching.current_source_id,
                system.ingest.emulation_mode
            )
            system.record_frame()

            if not should_continue:
                print("\nStopping by user input...")
                break

            time.sleep(0.03)

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
