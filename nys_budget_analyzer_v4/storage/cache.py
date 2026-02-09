"""
Extraction caching system for NYS Budget Analyzer.

Caches PDF extraction results to avoid re-parsing unchanged files.
Uses file hash + modification time for cache invalidation.
"""

import json
import hashlib
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime


class ExtractionCache:
    """
    Cache manager for PDF extraction results.

    Cache structure:
    ~/.nys_budget_cache/
        cache_index.json     # Maps file hashes to cache entries
        extractions/
            {hash}.json      # Cached extraction results

    Cache index entry:
    {
        "file_hash": "abc123...",
        "file_path": "/path/to/file.pdf",
        "file_size": 12345678,
        "extraction_time": "2026-02-08T14:30:00",
        "record_count": 5000,
        "version": "4.0"
    }
    """

    CACHE_VERSION = "4.0"

    def __init__(self, cache_dir: Optional[str] = None):
        """
        Initialize cache manager.

        Args:
            cache_dir: Custom cache directory. Defaults to ~/.nys_budget_cache/
        """
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        else:
            self.cache_dir = Path.home() / ".nys_budget_cache"

        self.extractions_dir = self.cache_dir / "extractions"
        self.index_path = self.cache_dir / "cache_index.json"

        # Ensure directories exist
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.extractions_dir.mkdir(parents=True, exist_ok=True)

        # Load or create index
        self.index = self._load_index()

    def _load_index(self) -> Dict[str, Dict[str, Any]]:
        """Load cache index from disk."""
        if self.index_path.exists():
            try:
                with open(self.index_path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    def _save_index(self):
        """Save cache index to disk."""
        with open(self.index_path, 'w') as f:
            json.dump(self.index, f, indent=2)

    def _compute_file_hash(self, file_path: str) -> str:
        """
        Compute SHA-256 hash of a file.

        Uses chunked reading for memory efficiency with large PDFs.
        """
        sha256 = hashlib.sha256()

        with open(file_path, 'rb') as f:
            # Read in 1MB chunks
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                sha256.update(chunk)

        return sha256.hexdigest()

    def get_cached(self, file_path: str) -> Optional[List[Dict[str, Any]]]:
        """
        Retrieve cached extraction results if available and valid.

        Args:
            file_path: Path to PDF file

        Returns:
            List of extracted records, or None if not cached/invalid
        """
        file_path = str(Path(file_path).resolve())

        # Check if file exists
        if not os.path.exists(file_path):
            return None

        # Compute hash
        file_hash = self._compute_file_hash(file_path)

        # Look up in index
        if file_hash not in self.index:
            return None

        entry = self.index[file_hash]

        # Validate version
        if entry.get('version') != self.CACHE_VERSION:
            # Cache from older version, invalidate
            self._remove_cache_entry(file_hash)
            return None

        # Load cached data
        cache_file = self.extractions_dir / f"{file_hash}.json"
        if not cache_file.exists():
            # Index entry exists but data missing, clean up
            self._remove_cache_entry(file_hash)
            return None

        try:
            with open(cache_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            self._remove_cache_entry(file_hash)
            return None

    def set_cached(self, file_path: str, records: List[Dict[str, Any]]):
        """
        Cache extraction results for a file.

        Args:
            file_path: Path to PDF file
            records: Extracted records to cache
        """
        file_path = str(Path(file_path).resolve())
        file_hash = self._compute_file_hash(file_path)

        # Save extraction data
        cache_file = self.extractions_dir / f"{file_hash}.json"
        with open(cache_file, 'w') as f:
            json.dump(records, f)

        # Update index
        self.index[file_hash] = {
            'file_hash': file_hash,
            'file_path': file_path,
            'file_name': os.path.basename(file_path),
            'file_size': os.path.getsize(file_path),
            'extraction_time': datetime.now().isoformat(),
            'record_count': len(records),
            'version': self.CACHE_VERSION
        }

        self._save_index()

    def _remove_cache_entry(self, file_hash: str):
        """Remove a cache entry and its data file."""
        if file_hash in self.index:
            del self.index[file_hash]
            self._save_index()

        cache_file = self.extractions_dir / f"{file_hash}.json"
        if cache_file.exists():
            cache_file.unlink()

    def is_cached(self, file_path: str) -> bool:
        """
        Check if a file has valid cached results.

        Args:
            file_path: Path to PDF file

        Returns:
            True if valid cache exists
        """
        file_path = str(Path(file_path).resolve())

        if not os.path.exists(file_path):
            return False

        file_hash = self._compute_file_hash(file_path)

        if file_hash not in self.index:
            return False

        if self.index[file_hash].get('version') != self.CACHE_VERSION:
            return False

        cache_file = self.extractions_dir / f"{file_hash}.json"
        return cache_file.exists()

    def get_cache_info(self, file_path: str) -> Optional[Dict[str, Any]]:
        """
        Get metadata about a cached extraction.

        Args:
            file_path: Path to PDF file

        Returns:
            Cache metadata dict, or None if not cached
        """
        file_path = str(Path(file_path).resolve())

        if not os.path.exists(file_path):
            return None

        file_hash = self._compute_file_hash(file_path)
        return self.index.get(file_hash)

    def clear_cache(self, file_path: Optional[str] = None):
        """
        Clear cache entries.

        Args:
            file_path: If provided, clear only this file's cache.
                      If None, clear all cached data.
        """
        if file_path:
            file_path = str(Path(file_path).resolve())
            if os.path.exists(file_path):
                file_hash = self._compute_file_hash(file_path)
                self._remove_cache_entry(file_hash)
        else:
            # Clear all
            for hash_key in list(self.index.keys()):
                self._remove_cache_entry(hash_key)

    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dict with cache statistics
        """
        total_records = sum(
            entry.get('record_count', 0)
            for entry in self.index.values()
        )

        total_size = sum(
            (self.extractions_dir / f"{h}.json").stat().st_size
            for h in self.index.keys()
            if (self.extractions_dir / f"{h}.json").exists()
        )

        return {
            'cache_dir': str(self.cache_dir),
            'cached_files': len(self.index),
            'total_records': total_records,
            'cache_size_bytes': total_size,
            'cache_size_mb': round(total_size / (1024 * 1024), 2),
            'version': self.CACHE_VERSION
        }

    def list_cached(self) -> List[Dict[str, Any]]:
        """
        List all cached extractions.

        Returns:
            List of cache entry metadata
        """
        return list(self.index.values())


def get_cache(cache_dir: Optional[str] = None) -> ExtractionCache:
    """
    Get a cache instance.

    Args:
        cache_dir: Optional custom cache directory

    Returns:
        ExtractionCache instance
    """
    return ExtractionCache(cache_dir)
