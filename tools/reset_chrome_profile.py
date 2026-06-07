import os
import shutil

import config


def main() -> None:
    profile_dir = os.path.abspath(config.CHROME_PROFILE_DIR)
    if not os.path.isdir(profile_dir):
        print(f"Profile directory does not exist: {profile_dir}")
        return
    shutil.rmtree(profile_dir, ignore_errors=True)
    print(f"Deleted Chrome profile directory: {profile_dir}")


if __name__ == "__main__":
    main()
