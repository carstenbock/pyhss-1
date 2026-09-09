# Copyright 2025 PyHSS Contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
IFC Template Cache Implementation

Provides thread-safe caching of compiled Jinja2 templates for both
database-based and file-based IFC templates.
"""

import threading
import jinja2
import os
from typing import Optional, Dict, Any
from database import IFC_TEMPLATE


class IfcTemplateCache:
    """
    Thread-safe cache for compiled Jinja2 IFC templates.
    
    Supports both database-based templates (when use_database=True) and
    file-based templates (when use_database=False) for backward compatibility.
    
    Cache keys:
    - For DB mode: "db:{template_id}"
    - For file mode: "file:{file_path}"
    """
    
    def __init__(self, logTool=None, redisMessaging=None):
        """
        Initialize the template cache.
        
        Args:
            logTool: Logger instance for logging messages
            redisMessaging: Redis messaging instance for pub/sub invalidation
        """
        self._cache: Dict[str, jinja2.Template] = {}
        self._lock = threading.Lock()
        self.logTool = logTool
        self.redisMessaging = redisMessaging
        # File system loader for file-based templates
        self._file_loaders: Dict[str, jinja2.FileSystemLoader] = {}
    
    def _log(self, level: str, message: str):
        """Helper to log messages if logTool is available."""
        if self.logTool:
            self.logTool.log(service='HSS', level=level, message=message, redisClient=self.redisMessaging)
    
    def _get_cache_key_db(self, template_id: int) -> str:
        """Generate cache key for database-based template."""
        return f"db:{template_id}"
    
    def _get_cache_key_file(self, file_path: str) -> str:
        """Generate cache key for file-based template."""
        return f"file:{file_path}"
    
    def get_template_from_db(self, template_id: int, database) -> Optional[jinja2.Template]:
        """
        Get a compiled template from the database.
        
        Args:
            template_id: ID of the template in the database
            database: Database instance to query
            
        Returns:
            Compiled Jinja2 template or None if not found
        """
        cache_key = self._get_cache_key_db(template_id)
        
        with self._lock:
            if cache_key in self._cache:
                self._log('debug', f"Template cache hit for db template {template_id}")
                return self._cache[cache_key]
        
        # Cache miss - load from database
        self._log('debug', f"Template cache miss for db template {template_id}, loading from database")
        
        try:
            template_data = database.GetObj(IFC_TEMPLATE, template_id)
            if template_data and 'template_content' in template_data:
                template_content = template_data['template_content']
                compiled_template = jinja2.Template(template_content)
                
                with self._lock:
                    self._cache[cache_key] = compiled_template
                
                self._log('debug', f"Template {template_id} compiled and cached")
                return compiled_template
            else:
                self._log('error', f"Template {template_id} not found in database")
                return None
        except Exception as e:
            self._log('error', f"Error loading template {template_id} from database: {str(e)}")
            return None
    
    def get_template_from_file(self, file_path: str, search_path: str = "../") -> Optional[jinja2.Template]:
        """
        Get a compiled template from the filesystem.
        
        Args:
            file_path: Path to the template file (relative to search_path)
            search_path: Base directory for template search
            
        Returns:
            Compiled Jinja2 template or None if not found
        """
        cache_key = self._get_cache_key_file(file_path)
        
        with self._lock:
            if cache_key in self._cache:
                self._log('debug', f"Template cache hit for file template {file_path}")
                return self._cache[cache_key]
        
        # Cache miss - load from file
        self._log('debug', f"Template cache miss for file template {file_path}, loading from filesystem")
        
        try:
            # Create or reuse file loader for this search path
            if search_path not in self._file_loaders:
                self._file_loaders[search_path] = jinja2.FileSystemLoader(searchpath=search_path)
            
            env = jinja2.Environment(loader=self._file_loaders[search_path])
            template = env.get_template(file_path)
            
            with self._lock:
                self._cache[cache_key] = template
            
            self._log('debug', f"Template {file_path} compiled and cached")
            return template
        except Exception as e:
            self._log('error', f"Error loading template {file_path} from filesystem: {str(e)}")
            return None
    
    def get_template(self, subscriber_details: Dict[str, Any], config: Dict[str, Any], database=None) -> Optional[jinja2.Template]:
        """
        Get the appropriate template for a subscriber based on configuration.
        
        This method implements the logic to choose between database-based and
        file-based templates based on the configuration and subscriber settings.
        
        Args:
            subscriber_details: Dictionary containing subscriber info (ifc_template_id, ifc_path)
            config: Application configuration dictionary
            database: Database instance (required when use_database=True)
            
        Returns:
            Compiled Jinja2 template or None if not found
        """
        ifc_config = config.get('hss', {}).get('ifc_templates', {})
        use_database = ifc_config.get('use_database', False)
        default_template_path = ifc_config.get('default_template_path', 'default_ifc.xml')
        
        # Check if we should use database-based templates
        if use_database:
            # Try to get template_id from subscriber
            template_id = subscriber_details.get('ifc_template_id')
            if template_id and database:
                template = self.get_template_from_db(template_id, database)
                if template:
                    return template
                self._log('warning', f"Failed to load db template {template_id}, falling back to file-based")
        
        # Fall back to file-based template
        ifc_path = subscriber_details.get('ifc_path') or default_template_path
        return self.get_template_from_file(ifc_path)
    
    def invalidate(self, cache_key: str) -> bool:
        """
        Invalidate a specific template from the cache.
        
        Args:
            cache_key: The cache key to invalidate (e.g., "db:123" or "file:default_ifc.xml")
            
        Returns:
            True if the key was found and removed, False otherwise
        """
        with self._lock:
            if cache_key in self._cache:
                del self._cache[cache_key]
                self._log('debug', f"Template cache invalidated: {cache_key}")
                return True
            return False
    
    def invalidate_db_template(self, template_id: int) -> bool:
        """
        Invalidate a database template from the cache.
        
        Args:
            template_id: ID of the template to invalidate
            
        Returns:
            True if the template was found and removed, False otherwise
        """
        cache_key = self._get_cache_key_db(template_id)
        return self.invalidate(cache_key)
    
    def invalidate_file_template(self, file_path: str) -> bool:
        """
        Invalidate a file template from the cache.
        
        Args:
            file_path: Path of the template to invalidate
            
        Returns:
            True if the template was found and removed, False otherwise
        """
        cache_key = self._get_cache_key_file(file_path)
        return self.invalidate(cache_key)
    
    def invalidate_all(self) -> int:
        """
        Clear the entire template cache.
        
        Returns:
            Number of templates that were invalidated
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._log('debug', f"Template cache cleared: {count} templates invalidated")
            return count
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the cache.
        
        Returns:
            Dictionary with cache statistics
        """
        with self._lock:
            db_templates = sum(1 for k in self._cache.keys() if k.startswith("db:"))
            file_templates = sum(1 for k in self._cache.keys() if k.startswith("file:"))
            return {
                "total_cached": len(self._cache),
                "db_templates": db_templates,
                "file_templates": file_templates,
                "cache_keys": list(self._cache.keys())
            }


# Singleton instance for global access
_template_cache_instance: Optional[IfcTemplateCache] = None
_instance_lock = threading.Lock()


def get_template_cache(logTool=None, redisMessaging=None) -> IfcTemplateCache:
    """
    Get or create the singleton template cache instance.
    
    Args:
        logTool: Logger instance (only used when creating new instance)
        redisMessaging: Redis messaging instance (only used when creating new instance)
        
    Returns:
        The singleton IfcTemplateCache instance
    """
    global _template_cache_instance
    
    if _template_cache_instance is None:
        with _instance_lock:
            if _template_cache_instance is None:
                _template_cache_instance = IfcTemplateCache(logTool, redisMessaging)
    
    return _template_cache_instance
