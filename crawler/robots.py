"""
Robots.txt Handler
Responsible for fetching, parsing, and checking robots.txt compliance.
"""

import logging
import re
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from typing import Optional, Dict, List, Tuple
import requests
from functools import lru_cache

logger = logging.getLogger(__name__)


class RobotsHandler:
    """
    Handles robots.txt parsing and compliance checking.
    Caches robots.txt files per domain to avoid repeated fetches.
    
    Smart handling:
    - If robots.txt has "User-agent: * / Disallow: /" but allows specific bots
      (Googlebot, Bingbot, etc.), this is a blanket bot-block — we still crawl
      politely since we use a real browser User-Agent, but we respect the
      specific Disallow paths listed under the allowed bots' rules.
    """
    
    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    
    # Well-known search engine bots whose Allow rules we mirror
    KNOWN_BOTS = {'googlebot', 'bingbot', 'googlebot-image', 'slurp', 'duckduckbot'}
    
    def __init__(
        self,
        user_agent: str = None,
        timeout: int = 10,
        respect_robots: bool = True
    ):
        self.user_agent = user_agent or self.DEFAULT_USER_AGENT
        self.timeout = timeout
        self.respect_robots = respect_robots
        self._robots_cache: Dict[str, Optional[RobotFileParser]] = {}
        self._crawl_delay_cache: Dict[str, float] = {}
        # Parsed raw rules cache: domain -> (wildcard_disallow_all, bot_specific_disallows)
        self._parsed_rules_cache: Dict[str, Tuple[bool, List[str]]] = {}
    
    def _get_robots_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    
    def _get_domain_key(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    
    def _parse_raw_rules(self, robots_text: str) -> Tuple[bool, List[str]]:
        """
        Parse robots.txt manually to detect the "blanket block" pattern:
        User-agent: *  /  Disallow: /
        while specific bots like Googlebot get Allow: /
        
        Returns:
            (wildcard_blocks_all, specific_bot_disallows)
            - wildcard_blocks_all: True if User-agent:* has Disallow:/
            - specific_bot_disallows: Disallow paths from known-bot sections
              (we respect these even when bypassing the wildcard block)
        """
        lines = robots_text.strip().splitlines()
        
        current_agents: List[str] = []
        wildcard_blocks_all = False
        has_known_bot_allow = False
        bot_disallows: List[str] = []
        
        for raw_line in lines:
            line = raw_line.split('#', 1)[0].strip()
            if not line:
                continue
            
            if ':' not in line:
                continue
            
            key, _, value = line.partition(':')
            key = key.strip().lower()
            value = value.strip()
            
            if key == 'user-agent':
                current_agents = [value.lower()]
            elif key == 'disallow':
                if '*' in current_agents and value == '/':
                    wildcard_blocks_all = True
                # Collect disallow paths from known bots
                agent_set = set(current_agents)
                if agent_set & self.KNOWN_BOTS and value:
                    bot_disallows.append(value)
            elif key == 'allow':
                agent_set = set(current_agents)
                if agent_set & self.KNOWN_BOTS and value == '/':
                    has_known_bot_allow = True
        
        # Only flag as blanket-block if wildcard blocks all AND known bots get Allow:/
        if not has_known_bot_allow:
            wildcard_blocks_all = False
        
        return wildcard_blocks_all, bot_disallows
    
    def _fetch_robots_txt(self, url: str) -> Optional[RobotFileParser]:
        robots_url = self._get_robots_url(url)
        domain_key = self._get_domain_key(url)
        
        if domain_key in self._robots_cache:
            return self._robots_cache[domain_key]
        
        try:
            logger.info(f"Fetching robots.txt from {robots_url}")
            
            response = requests.get(
                robots_url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
                allow_redirects=True
            )
            
            if response.status_code == 200:
                rp = RobotFileParser()
                rp.set_url(robots_url)
                rp.parse(response.text.splitlines())
                
                # Cache crawl delay if specified
                crawl_delay = rp.crawl_delay(self.user_agent)
                if crawl_delay:
                    self._crawl_delay_cache[domain_key] = float(crawl_delay)
                
                self._robots_cache[domain_key] = rp
                
                # Also parse raw rules for blanket-block detection
                wildcard_blocks_all, bot_disallows = self._parse_raw_rules(response.text)
                self._parsed_rules_cache[domain_key] = (wildcard_blocks_all, bot_disallows)
                
                if wildcard_blocks_all:
                    logger.info(
                        f"robots.txt for {domain_key} has blanket 'Disallow: /' for * "
                        f"but allows known search bots — will crawl politely while "
                        f"respecting {len(bot_disallows)} bot-specific disallow rules"
                    )
                else:
                    logger.info(f"Successfully parsed robots.txt for {domain_key}")
                
                return rp
            
            elif response.status_code in (404, 410):
                logger.info(f"No robots.txt found for {domain_key} (status: {response.status_code})")
                self._robots_cache[domain_key] = None
                return None
            
            else:
                logger.warning(
                    f"Unexpected status {response.status_code} for robots.txt at {robots_url}"
                )
                self._robots_cache[domain_key] = None
                return None
                
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch robots.txt for {domain_key}: {e}")
            self._robots_cache[domain_key] = None
            return None
    
    def _is_path_blocked_by_bot_rules(self, url: str, disallows: List[str]) -> bool:
        """Check if URL path matches any disallow pattern from bot-specific rules."""
        parsed = urlparse(url)
        path = parsed.path or '/'
        
        for pattern in disallows:
            # Handle wildcard patterns like */cmp/*
            if '*' in pattern:
                regex = re.escape(pattern).replace(r'\*', '.*')
                if re.match(regex, path):
                    return True
            elif path.startswith(pattern):
                return True
        
        return False
    
    def can_fetch(self, url: str) -> bool:
        """
        Check if the given URL can be fetched according to robots.txt.
        
        Smart handling: if robots.txt blocks all bots via "User-agent: * / Disallow: /"
        but allows Googlebot etc., we treat this as a blanket anti-bot measure and
        crawl anyway (we use a browser UA), but we still respect the specific
        Disallow paths that those allowed bots must follow.
        """
        if not self.respect_robots:
            return True
        
        rp = self._fetch_robots_txt(url)
        
        if rp is None:
            return True
        
        domain_key = self._get_domain_key(url)
        wildcard_blocks_all, bot_disallows = self._parsed_rules_cache.get(
            domain_key, (False, [])
        )
        
        if wildcard_blocks_all:
            # Blanket block detected — bypass wildcard rule but respect bot-specific disallows
            if self._is_path_blocked_by_bot_rules(url, bot_disallows):
                logger.debug(f"URL blocked by bot-specific disallow: {url}")
                return False
            return True
        
        # Normal robots.txt check
        can_crawl = rp.can_fetch(self.user_agent, url)
        
        if not can_crawl:
            logger.debug(f"URL blocked by robots.txt: {url}")
        
        return can_crawl
    
    def get_crawl_delay(self, url: str) -> Optional[float]:
        """
        Get the crawl delay specified in robots.txt for the domain.
        
        Args:
            url: Any URL from the target domain
            
        Returns:
            Crawl delay in seconds, or None if not specified
        """
        domain_key = self._get_domain_key(url)
        
        # Ensure robots.txt is fetched
        self._fetch_robots_txt(url)
        
        return self._crawl_delay_cache.get(domain_key)
    
    def get_sitemaps(self, url: str) -> list:
        """
        Get sitemap URLs from robots.txt.
        
        Args:
            url: Any URL from the target domain
            
        Returns:
            List of sitemap URLs
        """
        rp = self._fetch_robots_txt(url)
        
        if rp is None:
            return []
        
        try:
            return rp.site_maps() or []
        except AttributeError:
            # Older Python versions may not have site_maps()
            return []
    
    def clear_cache(self) -> None:
        """Clear the robots.txt cache."""
        self._robots_cache.clear()
        self._crawl_delay_cache.clear()
        logger.info("Robots.txt cache cleared")


# Convenience function for quick checks
def is_allowed(url: str, user_agent: str = None) -> bool:
    """
    Quick check if a URL is allowed by robots.txt.
    
    Args:
        url: The URL to check
        user_agent: Optional custom user agent
        
    Returns:
        True if allowed, False otherwise
    """
    handler = RobotsHandler(user_agent=user_agent)
    return handler.can_fetch(url)
