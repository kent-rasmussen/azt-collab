"""Client-side Project dataclass (decode only). Mirrors
azt_collabd.projects.Project."""

from dataclasses import dataclass


@dataclass
class Project:
    langcode: str
    working_dir: str
    lift_path: str = ''
    remote_url: str = ''
    last_sync: float = 0.0
    created_at: float = 0.0

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            langcode=d.get('langcode', ''),
            working_dir=d.get('working_dir', ''),
            lift_path=d.get('lift_path', ''),
            remote_url=d.get('remote_url', ''),
            last_sync=float(d.get('last_sync', 0.0)),
            created_at=float(d.get('created_at', 0.0)),
        )


@dataclass
class ProjectStatus:
    """Snapshot of one project's git state."""
    langcode: str
    branch: str
    remote_url: str
    n_changes: int
    last_sync: float
    working_dir: str
    lift_path: str

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            langcode=d.get('langcode', ''),
            branch=d.get('branch', ''),
            remote_url=d.get('remote_url', ''),
            n_changes=int(d.get('n_changes', 0)),
            last_sync=float(d.get('last_sync', 0.0)),
            working_dir=d.get('working_dir', ''),
            lift_path=d.get('lift_path', ''),
        )
