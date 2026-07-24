import os
import logging
import sys
import csv
import json
from pathlib import Path
from typing import Set, List, Optional, Dict, Any
from datetime import datetime, timezone # Added datetime and timezone

# Adjust import path for ConfigLoader and setup_logger
try:
    from ..core.config_loader import ConfigLoader, PROJECT_ROOT
    from .logger import setup_logger # Assuming logger.py is in the same utils directory
except ImportError:
    # This block allows the script to be run directly for testing,
    # assuming the script is in src/utils and the root is two levels up.
    sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
    from xuse.core.config_loader import ConfigLoader, PROJECT_ROOT
    from xuse.utils.logger import setup_logger

config_loader_instance = ConfigLoader() # Initialize once
# Initialize logging configuration and get a module-specific logger
setup_logger(config_loader_instance)
logger = logging.getLogger(__name__)

# PROJECT_ROOT is imported from xuse.core.config_loader (single source of truth).

class FileHandler:
    def __init__(self, config_loader: Optional[ConfigLoader] = None):
        if config_loader is None:
            self.config_loader = config_loader_instance
        else:
            self.config_loader = config_loader
        
        # Read the whole twitter_automation block. (An empty setting name here
        # would build the path "twitter_automation." and never resolve.)
        twitter_auto_settings = self.config_loader.get_setting('twitter_automation', {})
        self.twitter_auto_settings: Dict[str, Any] = (
            twitter_auto_settings if isinstance(twitter_auto_settings, dict) else {}
        )
        
        # Get processed_tweets_file path from config, default if not found
        processed_tweets_file_rel_path = self.twitter_auto_settings.get('processed_tweets_file', 'processed_tweets_log.csv')
        
        # Ensure the path is Path object and resolve it relative to project root
        self.processed_tweets_file_path: Path = PROJECT_ROOT / processed_tweets_file_rel_path
        
        # Ensure the directory for the processed tweets file exists
        self.ensure_directory_exists(self.processed_tweets_file_path.parent)

    def ensure_directory_exists(self, dir_path: Path) -> None:
        """Ensures that the specified directory exists, creating it if necessary."""
        try:
            if not dir_path.exists():
                dir_path.mkdir(parents=True, exist_ok=True)
                logger.info(f"Created directory: {dir_path}")
            elif not dir_path.is_dir():
                logger.error(f"Path exists but is not a directory: {dir_path}")
                raise NotADirectoryError(f"{dir_path} exists but is not a directory.")
        except OSError as e:
            logger.error(f"Error creating directory {dir_path}: {e}")
            raise

    def load_processed_action_keys(self) -> Set[str]:
        """
        Loads processed action_keys from the CSV file.
        Each action_key should be unique and typically combines action type, account ID, and tweet ID.

        Dedup is PERMANENT: every row loads regardless of its date. The MCP
        server restarts on every client launch and reloads this set — a
        same-day filter silently expired every key at midnight UTC while the
        tool messages promise permanent dedup. Timestamps are still parsed
        tolerantly (naive values are normalized to UTC so naive and aware
        rows never mix), but a timestamp is never a reason to drop a key.
        """
        processed_keys: Set[str] = set()
        if not self.processed_tweets_file_path.exists():
            logger.info(f"Processed actions file not found at {self.processed_tweets_file_path}. Starting with an empty set.")
            return processed_keys

        try:
            timestamp_col_idx = -1

            with self.processed_tweets_file_path.open(mode='r', newline='', encoding='utf-8') as file:
                reader = csv.reader(file)
                header = next(reader, None)
                if header is None:
                    logger.warning(f"Processed actions file is empty: {self.processed_tweets_file_path}")
                    return processed_keys

                # Try to find the 'timestamp' column index
                try:
                    timestamp_col_idx = header.index('timestamp')
                except ValueError:
                    logger.warning(f"'timestamp' column not found in header of {self.processed_tweets_file_path}. Loading all keys.")
                    # Fallback to loading all keys if no timestamp column
                    for row in reader:
                        if row and row[0]: processed_keys.add(row[0])
                    logger.info(f"Loaded {len(processed_keys)} processed action keys (all, no timestamp column) from {self.processed_tweets_file_path}")
                    return processed_keys

                for row in reader:
                    if not row or not row[0]:
                        continue # Skip empty rows

                    action_key = row[0]
                    if len(row) > timestamp_col_idx:
                        timestamp_str = row[timestamp_col_idx]
                        try:
                            action_datetime = datetime.fromisoformat(timestamp_str)
                            # Normalize naive timestamps to UTC so naive-local
                            # and aware-UTC rows never mix (writers have used
                            # both). Loading never filters by date, so this
                            # choice cannot drop a key.
                            if action_datetime.tzinfo is None:
                                action_datetime = action_datetime.replace(tzinfo=timezone.utc)
                        except ValueError:
                            # Keep the key anyway: a malformed timestamp must
                            # not reopen a duplicate-write window.
                            logger.warning(f"Could not parse timestamp '{timestamp_str}' for action_key '{action_key}'. Keeping the key (dedup is permanent).")
                    processed_keys.add(action_key)

            logger.info(f"Loaded {len(processed_keys)} processed action keys (all rows, permanent dedup) from {self.processed_tweets_file_path}")
        except StopIteration: # Handles empty file after header read attempt
             logger.warning(f"Processed actions file {self.processed_tweets_file_path} contains only a header or is empty.")
        except csv.Error as e:
            logger.error(f"CSV Error reading {self.processed_tweets_file_path}: {e}")
        except Exception as e:
            logger.error(f"Error loading processed action keys from {self.processed_tweets_file_path}: {e}")
        return processed_keys

    def save_processed_action_key(self, action_key: str, timestamp: Optional[str] = None, **extra_data: Any) -> bool:
        """
        Appends a processed action_key and optional timestamp and extra data to the CSV file.
        Returns True on success, False on failure.
        """
        try:
            file_exists_and_not_empty = self.processed_tweets_file_path.exists() and self.processed_tweets_file_path.stat().st_size > 0
            
            # Define base header
            current_header = ['action_key']
            if timestamp is not None:
                current_header.append('timestamp')
            
            # Incorporate keys from extra_data into the header if they are not already there
            # This part is tricky if we want to dynamically add columns to an existing CSV.
            # For simplicity, we'll assume a fixed header for now or that extra_data keys are consistent.
            # A more robust solution would involve reading the existing header and merging.
            # For this iteration, we'll keep it simple: if extra_data is provided, its keys are expected.
            
            # If we want dynamic columns, we'd need to read the header first if file exists.
            # For now, let's assume the initial save operation defines the columns if extra_data is present.
            
            row_to_write = [action_key]
            if timestamp is not None:
                row_to_write.append(timestamp)

            # Handle extra_data: For simplicity, we'll just append them.
            # A more robust CSV handler might map them to specific columns.
            # This current implementation might lead to misaligned columns if not all rows have all extra_data keys.
            # Consider using a dictionary writer for more complex CSVs.
            header_needs_update = False
            final_header = list(current_header) # Start with a copy

            if extra_data:
                for key in extra_data.keys():
                    if key not in final_header:
                        final_header.append(key) # Add new keys to header
                        header_needs_update = True # Mark that header might need rewrite if file exists
                
                for key in final_header: # Ensure order for row_to_write
                    if key in ['action_key', 'timestamp']: continue # Already handled
                    row_to_write.append(extra_data.get(key, '')) # Append value or empty string

            # If the file is new, or empty, or header needs update (and we decide to rewrite for simplicity)
            # Rewriting a CSV to add columns is complex. Let's simplify:
            # If new columns are introduced by extra_data, and the file exists, this basic append might misalign.
            # For now, we'll write the header if the file is new/empty.
            # If extra_data introduces new columns to an existing file, this simple append won't update the header row.

            with self.processed_tweets_file_path.open(mode='a', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                if not file_exists_and_not_empty:
                    writer.writerow(final_header) # Write header if file is new or empty
                
                writer.writerow(row_to_write)
            logger.debug(f"Saved action_key: {action_key} with timestamp: {timestamp} and extra_data to {self.processed_tweets_file_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving action_key {action_key} to {self.processed_tweets_file_path}: {e}")
            return False

    # --- Generic File Utilities ---

    def read_text(self, file_path: Path) -> Optional[str]:
        """Reads entire content from a text file."""
        try:
            if not file_path.is_file():
                logger.warning(f"Text file not found: {file_path}")
                return None
            content = file_path.read_text(encoding='utf-8')
            logger.debug(f"Successfully read text file: {file_path}")
            return content
        except Exception as e:
            logger.error(f"Error reading text file {file_path}: {e}")
            return None

    def write_text(self, file_path: Path, content: str, append: bool = False) -> bool:
        """Writes or appends content to a text file. Ensures directory exists."""
        try:
            self.ensure_directory_exists(file_path.parent)
            mode = 'a' if append else 'w'
            with file_path.open(mode=mode, encoding='utf-8') as file:
                file.write(content)
            logger.debug(f"Successfully {'appended to' if append else 'wrote to'} text file: {file_path}")
            return True
        except Exception as e:
            logger.error(f"Error writing to text file {file_path}: {e}")
            return False

    def read_json(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """Reads data from a JSON file."""
        try:
            if not file_path.is_file():
                logger.warning(f"JSON file not found: {file_path}")
                return None
            with file_path.open('r', encoding='utf-8') as file:
                data = json.load(file)
            logger.debug(f"Successfully read JSON file: {file_path}")
            return data
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from file {file_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error reading JSON file {file_path}: {e}")
            return None

    def write_json(self, file_path: Path, data: Dict[str, Any], indent: int = 4) -> bool:
        """Writes data to a JSON file. Ensures directory exists."""
        try:
            self.ensure_directory_exists(file_path.parent)
            with file_path.open('w', encoding='utf-8') as file:
                json.dump(data, file, indent=indent, ensure_ascii=False)
            logger.debug(f"Successfully wrote JSON to file: {file_path}")
            return True
        except Exception as e:
            logger.error(f"Error writing JSON to file {file_path}: {e}")
            return False

    def list_files(self, directory_path: Path, pattern: str = "*") -> List[Path]:
        """Lists files in a directory, optionally matching a glob pattern."""
        try:
            if not directory_path.is_dir():
                logger.warning(f"Directory not found for listing files: {directory_path}")
                return []
            files = list(directory_path.glob(pattern))
            logger.debug(f"Found {len(files)} files in {directory_path} matching '{pattern}'.")
            return files
        except Exception as e:
            logger.error(f"Error listing files in directory {directory_path}: {e}")
            return []
            
    def delete_file(self, file_path: Path) -> bool:
        """Deletes a file."""
        try:
            if not file_path.is_file():
                logger.warning(f"File not found for deletion: {file_path}")
                return False
            file_path.unlink()
            logger.info(f"Successfully deleted file: {file_path}")
            return True
        except Exception as e:
            logger.error(f"Error deleting file {file_path}: {e}")
            return False


if __name__ == '__main__':
    # Example Usage
    # Ensure config is loaded for logger, or provide a mock/simple config for testing
    # For simplicity, we assume config_loader_instance and logger are set up globally
    
    # Create a temporary directory for testing relative to this script file
    test_dir = Path(__file__).parent / "test_file_handler_output"
    if not test_dir.exists():
        test_dir.mkdir(parents=True, exist_ok=True)

    # Override processed_tweets_file_path for testing to be inside test_dir
    # This requires a bit of a workaround if we want to test FileHandler's own config reading
    # For a direct test, we can instantiate FileHandler and then change its path attribute
    
    # A more direct way to test is to mock ConfigLoader or provide a test config file.
    # For this example, let's assume the default 'processed_tweets_log.csv' will be created in PROJECT_ROOT
    # or we can manually set the path for the test instance.
    
    # To avoid polluting the project root, let's create a custom config for testing
    # or simply use the new methods with explicit paths within our test_dir.

    print(f"--- FileHandler Tests (output in: {test_dir.resolve()}) ---")
    
    # Instantiate FileHandler - it will use global config_loader_instance
    # Its processed_tweets_file_path will be based on your actual project config or default.
    # For isolated testing of CSV methods, let's make a specific test file path.
    
    file_handler = FileHandler() # Uses global config
    
    # For processed_actions_keys, let's use a dedicated test file
    original_processed_path = file_handler.processed_tweets_file_path
    test_csv_path = test_dir / "test_processed_actions.csv"
    file_handler.processed_tweets_file_path = test_csv_path # Override for test
    
    # Clean up previous test CSV if it exists
    if test_csv_path.exists():
        test_csv_path.unlink()

    print(f"\n--- Testing Processed Action Keys (CSV at {test_csv_path}) ---")
    # Test loading (file might not exist initially)
    print("--- Testing Load (Initial) ---")
    initial_keys = file_handler.load_processed_action_keys()
    print(f"Initial processed action_keys: {initial_keys}")

    # Test saving
    print("\n--- Testing Save ---")
    from datetime import datetime
    ts = datetime.now().isoformat()
    file_handler.save_processed_action_key("reply_user1_tweet123", timestamp=ts, source="test_script")
    file_handler.save_processed_action_key("like_user1_tweet456", timestamp=ts, source="test_script", attempts=1)
    file_handler.save_processed_action_key("repost_user2_tweet789", source="another_test") # No timestamp, different extra data

    # Test loading again
    print("\n--- Testing Load (After Save) ---")
    updated_keys = file_handler.load_processed_action_keys()
    print(f"Updated processed action_keys: {updated_keys}")
    print(f"Check the file: {test_csv_path}")

    # --- Test Generic Utilities ---
    print("\n--- Testing Generic File Utilities ---")

    # Test Text Files
    test_text_file = test_dir / "sample.txt"
    print(f"\n--- Testing Text File: {test_text_file} ---")
    file_handler.write_text(test_text_file, "Hello, World!\n")
    file_handler.write_text(test_text_file, "This is a new line.", append=True)
    content = file_handler.read_text(test_text_file)
    print(f"Content of {test_text_file}:\n{content}")

    # Test JSON Files
    test_json_file = test_dir / "sample.json"
    print(f"\n--- Testing JSON File: {test_json_file} ---")
    json_data = {"name": "Test User", "id": 123, "active": True, "tags": ["test", "example"]}
    file_handler.write_json(test_json_file, json_data)
    read_data = file_handler.read_json(test_json_file)
    print(f"Content of {test_json_file}: {read_data}")
    
    # Test Listing Files
    print(f"\n--- Testing List Files in {test_dir} ---")
    # Create some dummy files for listing
    (test_dir / "file1.txt").touch()
    (test_dir / "file2.log").touch()
    (test_dir / "another.json").touch()
    
    all_files = file_handler.list_files(test_dir)
    print(f"All files in {test_dir}: {[f.name for f in all_files]}")
    
    txt_files = file_handler.list_files(test_dir, pattern="*.txt")
    print(f"Text files in {test_dir}: {[f.name for f in txt_files]}")

    json_files = file_handler.list_files(test_dir, pattern="*.json")
    print(f"JSON files in {test_dir}: {[f.name for f in json_files]}")

    # Test Deleting Files
    file_to_delete = test_dir / "file_to_delete.tmp"
    file_handler.write_text(file_to_delete, "This file will be deleted.")
    print(f"\n--- Testing Delete File: {file_to_delete} ---")
    if file_to_delete.exists():
        print(f"File {file_to_delete.name} exists before deletion.")
    else:
        print(f"File {file_to_delete.name} does NOT exist before deletion (ERROR IN TEST SETUP).")
        
    delete_success = file_handler.delete_file(file_to_delete)
    print(f"Deletion successful: {delete_success}")
    
    if file_to_delete.exists():
        print(f"File {file_to_delete.name} STILL exists after deletion (DELETION FAILED).")
    else:
        print(f"File {file_to_delete.name} no longer exists after deletion.")

    # Restore original path if it was changed for testing (though instance is local to main)
    file_handler.processed_tweets_file_path = original_processed_path

    print(f"\n--- End of FileHandler Tests ---")
    print(f"NOTE: You might want to manually delete the '{test_dir.resolve()}' directory after inspection.")
    # Example: To clean up test_dir (optional, be careful with rmtree)
    # import shutil
    # print(f"\nCleaning up test directory: {test_dir}")
    # shutil.rmtree(test_dir)
