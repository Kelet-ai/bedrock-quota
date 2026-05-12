"""Entry point for the Bedrock Quota TUI application."""

from .app import BedrockTUI


def main():
    """Launch the TUI application."""
    app = BedrockTUI()
    app.run()


if __name__ == "__main__":
    main()
