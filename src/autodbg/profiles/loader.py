from __future__ import annotations

from pathlib import Path
import tomllib

from autodbg.models.profile import DeviceProfile, ModelProfile, RunProfiles, TaskProfile, TransportProfile


class ProfileResolutionError(ValueError):
    """Raised when the effective profile set cannot be resolved."""


_PROFILE_KINDS = ("device", "model", "task", "transport")


def _read_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def default_profile_defaults_path(project_root: Path) -> Path:
    return project_root / "profiles" / "defaults.toml"


def load_profile_defaults(path: Path) -> dict[str, Path]:
    resolved_path = path.resolve()
    if not resolved_path.exists():
        raise ProfileResolutionError(f"Default profile manifest does not exist: {resolved_path}")
    raw = _read_toml(resolved_path)
    defaults = raw.get("profiles", {})
    missing = [kind for kind in _PROFILE_KINDS if not defaults.get(kind)]
    if missing:
        raise ProfileResolutionError(
            f"Default profile manifest is missing entries for: {', '.join(missing)}"
        )

    resolved: dict[str, Path] = {}
    for kind in _PROFILE_KINDS:
        candidate = Path(str(defaults[kind]))
        if not candidate.is_absolute():
            candidate = (resolved_path.parent / candidate).resolve()
        resolved[kind] = candidate
    return resolved


def resolve_run_profile_paths(
    project_root: Path,
    *,
    device_path: Path | None,
    model_path: Path | None,
    task_path: Path | None,
    transport_path: Path | None,
    defaults_path: Path | None = None,
) -> tuple[Path, Path, Path, Path]:
    defaults: dict[str, Path] = {}
    if any(path is None for path in (device_path, model_path, task_path, transport_path)):
        defaults = load_profile_defaults(defaults_path or default_profile_defaults_path(project_root))

    resolved_paths = {
        "device": device_path.resolve() if device_path is not None else defaults["device"],
        "model": model_path.resolve() if model_path is not None else defaults["model"],
        "task": task_path.resolve() if task_path is not None else defaults["task"],
        "transport": transport_path.resolve() if transport_path is not None else defaults["transport"],
    }
    missing_files = [f"{kind}={path}" for kind, path in resolved_paths.items() if not path.is_file()]
    if missing_files:
        raise ProfileResolutionError(
            "Resolved profile files do not exist: " + ", ".join(missing_files)
        )
    return (
        resolved_paths["device"],
        resolved_paths["model"],
        resolved_paths["task"],
        resolved_paths["transport"],
    )


def load_device_profile(path: Path) -> DeviceProfile:
    return DeviceProfile.from_dict(_read_toml(path), path.resolve())


def load_model_profile(path: Path) -> ModelProfile:
    return ModelProfile.from_dict(_read_toml(path), path.resolve())


def load_task_profile(path: Path) -> TaskProfile:
    return TaskProfile.from_dict(_read_toml(path), path.resolve())


def load_transport_profile(path: Path) -> TransportProfile:
    return TransportProfile.from_dict(_read_toml(path), path.resolve())


def load_run_profiles(
    device_path: Path,
    model_path: Path,
    task_path: Path,
    transport_path: Path,
) -> RunProfiles:
    device = load_device_profile(device_path)
    model = load_model_profile(model_path)
    task = load_task_profile(task_path)
    transport = load_transport_profile(transport_path)

    if device.model_id != model.model_id:
        raise ValueError(
            f"Device profile model_id={device.model_id} does not match model profile model_id={model.model_id}."
        )

    return RunProfiles(device=device, model=model, task=task, transport=transport)
