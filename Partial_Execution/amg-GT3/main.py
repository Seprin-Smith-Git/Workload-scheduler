import subprocess
from pathlib import Path

# Update this path to match your actual Blender version folder
BLENDER = r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
BLEND_FILE = str(Path("main.blend").resolve())

start_frame = 21
end_frame = 84

for frame in range(start_frame, end_frame + 1):
    print(f"Rendering frame {frame}")

    result = subprocess.run([
        BLENDER,
        "-b",
        BLEND_FILE,
        "-f",
        str(frame)
    ])

    if result.returncode != 0:
        print(f"Failed on frame {frame}")
        break