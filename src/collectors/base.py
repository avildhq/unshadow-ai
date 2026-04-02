from abc import ABC, abstractmethod

from src.models import ExtensionInfo


class BaseCollector(ABC):
    @abstractmethod
    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        """Collect extensions for the given users.

        Args:
            users: List of (username, home_directory_path) tuples.

        Returns:
            List of ExtensionInfo objects.
        """
