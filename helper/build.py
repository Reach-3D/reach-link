#!/usr/bin/env python3
"""
Build script for Reach-Link Helper Bridge Windows executable.

Usage:
    python build.py
    
Requirements:
    - Python 3.7+
    - pyinstaller: pip install pyinstaller
    - Dependencies: pip install -r requirements.txt
"""

import os
import sys
import subprocess
from pathlib import Path


def main():
    """Build Windows executable"""
    
    print("=" * 60)
    print("Reach-Link Helper Bridge - Build Script")
    print("=" * 60)

    helper_dir = Path(__file__).parent

    # Check Python version
    if sys.version_info < (3, 7):
        print(f"ERROR: Python 3.7+ required (current: {sys.version_info.major}.{sys.version_info.minor})")
        sys.exit(1)

    print(f"✓ Python version: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    # Install dependencies
    print("\nInstalling dependencies...")
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-r', str(helper_dir / 'requirements.txt')])
        print("✓ Dependencies installed")
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to install dependencies: {e}")
        sys.exit(1)

    # Install PyInstaller
    print("\nInstalling PyInstaller...")
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pyinstaller'])
        print("✓ PyInstaller installed")
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to install PyInstaller: {e}")
        sys.exit(1)

    # Clean previous build
    dist_dir = helper_dir / 'dist'
    build_dir = helper_dir / 'build'
    if dist_dir.exists():
        import shutil
        shutil.rmtree(dist_dir)
        print(f"✓ Cleaned dist directory")
    if build_dir.exists():
        import shutil
        shutil.rmtree(build_dir)
        print("✓ Cleaned build directory")

    # Run PyInstaller
    print("\nBuilding executable (this may take 1-2 minutes)...")
    spec_file = helper_dir / 'reach-link-helper.spec'
    
    try:
        os.chdir(helper_dir)
        subprocess.check_call([
            sys.executable, '-m', 'PyInstaller',
            str(spec_file),
            '--distpath', str(dist_dir),
            '--workpath', str(build_dir),
        ])
        print("✓ Build complete")
    except subprocess.CalledProcessError as e:
        print(f"ERROR: PyInstaller failed: {e}")
        sys.exit(1)

    # Check output
    exe_path = dist_dir / 'reach-link-helper.exe'
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"✓ Executable created: {exe_path}")
        print(f"  Size: {size_mb:.1f} MB")
        
        # Show usage instructions
        print("\n" + "=" * 60)
        print("Build successful!")
        print("=" * 60)
        print(f"\nExecutable: {exe_path}")
        print("\nUsage:")
        print("  1. Copy reach-link-helper.exe to a location in your PATH")
        print("  2. Run: reach-link-helper.exe")
        print("  3. Helper will start on localhost:5900 (or next available port)")
        print("\nThe browser printer setup wizard can now detect and use this helper.")
        print("=" * 60)
    else:
        print(f"ERROR: Executable not found at {exe_path}")
        sys.exit(1)


if __name__ == '__main__':
    main()
