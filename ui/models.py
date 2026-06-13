import json
from dataclasses import dataclass
from pathlib import Path

from config.settings import config_service
from ui.theme import CARD_GAP


@dataclass
class CameraAssignment:
    role: int
    video_id: int
    audio_id: int | None
    name: str
    video_name: str = ""
    audio_name: str = ""


@dataclass(frozen=True)
class CameraGridLayout:
    cols: int
    rows: int
    card_size: tuple[int, int]
    preview_size: tuple[int, int]
    top_pad: int = 0
    gap: int = CARD_GAP


class UserSettings:
    def __init__(self):
        self.path = Path.home() / ".intellicut" / "settings.json"
        self.data = {
            "camera_roles": {},
            "camera_roles_configured": False,
            "camera_slot_count": config_service.default_camera_slots,
            "output_folder": str(Path(config_service.output_path).parent),
        }
        self.load()

    def load(self):
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except Exception:
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def output_folder(self) -> Path:
        return Path(self.data.get("output_folder") or "output")

    def set_output_folder(self, folder: str):
        self.data["output_folder"] = folder
        config_service.output_path = str(Path(folder) / Path(config_service.output_path).name)
        self.save()

    def camera_slot_count(self) -> int:
        try:
            configured = int(self.data.get("camera_slot_count", config_service.default_camera_slots))
        except Exception:
            configured = config_service.default_camera_slots
        count = max(config_service.default_camera_slots, configured)
        return min(config_service.max_sources, count)

    def set_camera_slot_count(self, count: int, save: bool = True):
        count = max(config_service.default_camera_slots, min(config_service.max_sources, int(count)))
        self.data["camera_slot_count"] = count
        if save:
            self.save()

    def assignments(self) -> list[CameraAssignment]:
        roles = self.data.get("camera_roles") or {}
        assignments = []

        for key, value in roles.items():
            if not isinstance(value, dict):
                continue

            try:
                role = int(str(key).split("_")[-1])
                audio_id = value.get("audio_id")

                assignments.append(
                    CameraAssignment(
                        role=role,
                        video_id=int(value["video_id"]),
                        audio_id=int(audio_id) if audio_id is not None else None,
                        name=value.get("name") or f"Camera {role}",
                        video_name=value.get("video_name") or "",
                        audio_name=value.get("audio_name") or "",
                    )
                )
            except Exception:
                continue

        return sorted(assignments, key=lambda item: item.role)

    def save_assignments(self, assignments: list[CameraAssignment]):
        roles = {}

        for assignment in assignments:
            roles[f"camera_{assignment.role}"] = {
                "video_id": assignment.video_id,
                "video_name": assignment.video_name,
                "audio_id": assignment.audio_id,
                "audio_name": assignment.audio_name,
                "name": assignment.name,
            }

        self.data["camera_roles"] = roles
        self.data["camera_roles_configured"] = True

        max_role = max((assignment.role for assignment in assignments), default=0)
        self.data["camera_slot_count"] = max(
            self.camera_slot_count(),
            max_role,
            config_service.default_camera_slots,
        )

        self.save()
