"""
This module provides functionality for renaming files and directories
to comply with Plex naming conventions.
"""

import os
import re
import argparse
import logging

def format_name(entity, is_file=False, year_from_file=None):
    """Format the name of a file or directory according to specified patterns and conditions."""
    extension = ''
    if is_file:
        entity, extension = os.path.splitext(entity)

    entity = entity.replace('&', 'and')

    correct_format_pattern = r'^[\w\s]+ \(\d{4}\)$'
    if re.match(correct_format_pattern, os.path.splitext(entity)[0]):
        return entity + extension

    patterns_to_remove = [
        r'\[1080p\]', r'\[480p\]', r'\[WEBRip\]', r'\[BluRay\]',
        r'\[5\.1\]', r'\[YTS\.MX\]', r'\[2160p\]', r'\[4K\]', r'\[WEB\]',
        r'x264', r'x265', r'H264', r'H265', r'\.HDR\.', r'\.S\d+E\d+\.',
        r'\.AC3\.', r'\.EAC3\.', r'\.AAC\.', r'\.MP3\.', r'\.AVC\.', r'HEVC',
        r'-'
    ]

    name = re.sub(r'[\._-]+', ' ', entity)

    for pattern in patterns_to_remove:
        name = re.sub(pattern, '', name, flags=re.IGNORECASE)

    year_match = re.search(r'(\d{4})', name)
    year = year_match.group(1) if year_match else year_from_file

    if year:
        name = re.sub(r'(.*)\d{4}.*', rf'\1', name).strip()
        name = f"{name.strip()} ({year})"
    else:
        name = re.sub(r'\(\s*\)', '', name)

    name = re.sub(r'[-\s]+$', '', name).strip()
    name = re.sub(r'\s+', ' ', name).strip()

    new_name = f"{name}{extension}" if is_file else name
    return new_name

def rename_entity(root_path, entity, dry_run, ignored_dirs, is_file=True):
    """Rename an entity based on specified conditions and log actions accordingly."""
    if entity.startswith('.'):
        logging.info("Skipping hidden file or directory: %s", entity)
        return

    if os.path.basename(root_path) in ignored_dirs:
        logging.info("Skipping ignored directory: %s", root_path)
        return

    extension = os.path.splitext(entity)[1].lower()
    ignore_extensions = ['.vob', '.info', '.nfo', '.ifo', '.bup', '.log', '.py']

    if extension in ignore_extensions:
        logging.info("Skipping renaming of file with ignored extension: %s", entity)
        return

    if entity.startswith('._'):
        logging.info("Skipping file '%s' as it starts with '._'", entity)
        return

    original_path = os.path.join(root_path, entity)
    year_from_file = None

    if not is_file:
        for file in os.listdir(original_path):
            if file.startswith('.'):
                continue  # Skip hidden files inside directories
            file_year_match = re.search(r'(\d{4})', file)
            if file_year_match:
                year_from_file = file_year_match.group(1)
                break

    new_name = format_name(entity, is_file, year_from_file)
    new_path = os.path.join(root_path, new_name)

    if original_path != new_path:
        if dry_run:
            logging.info("Dry run: Would rename '%s' to '%s'", original_path, new_path)
        else:
            try:
                os.rename(original_path, new_path)
                logging.info("Renamed '%s' to '%s'", original_path, new_path)
            except OSError as e:
                logging.error("Error renaming '%s' to '%s': %s", original_path, new_path, e)
    else:
        logging.info("No renaming necessary for '%s'", original_path)

def walk_directory(directory, dry_run, ignored_dirs):
    """Walk through the directory and apply renaming rules to each file and directory."""
    for root, dirs, files in os.walk(directory, topdown=True):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ignored_dirs]  \
            # Skip hidden directories
        files = [f for f in files if not f.startswith('.')]  # Skip hidden files
        for name in files:
            rename_entity(root, name, dry_run, ignored_dirs, is_file=True)
        for name in dirs:
            rename_entity(root, name, dry_run, ignored_dirs, is_file=False)

def main():
    """Parse command-line arguments and initiate directory walking and renaming process."""
    parser = argparse.ArgumentParser(description="Rename files and directories \
        to comply with Plex naming conventions.")
    parser.add_argument("directory", type=str, help="The root directory to \
        start renaming from.")
    parser.add_argument("--dry-run", action="store_true", help="Run the script \
        in dry run mode without making any changes.")
    parser.add_argument("--ignore-dirs", nargs='*', default=[], help="List of directory \
        names to ignore during renaming.")
    parser.add_argument("--log", type=str, default="renaming.log", \
        help="Path to the log file.")
    args = parser.parse_args()

    logging.basicConfig(filename=args.log, level=logging.INFO, \
        format='%(asctime)s - %(levelname)s - %(message)s')
    logging.info("Starting renaming process...")
    if args.dry_run:
        logging.info("Running in dry-run mode. No changes will be made.")
    walk_directory(args.directory, args.dry_run, args.ignore_dirs)
    logging.info("Renaming process completed.")

if __name__ == "__main__":
    main()
