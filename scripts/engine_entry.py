"""PyInstaller entry point. Freeze support must precede the server import."""
from multiprocessing import freeze_support

if __name__ == '__main__':
    freeze_support()
    from paperweight.server import main
    main()
