"""Screen contract every ui/screens/*.py implements."""


class Screen:
    def on_enter(self) -> None:
        """Called once by Navigator.go_to right before this screen becomes current."""

    def draw(self, navigator) -> None:
        raise NotImplementedError
