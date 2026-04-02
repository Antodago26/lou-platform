from .base import BaseScraper
from .homegate import HomegateScraper
from .immoscout import ImmoScoutScraper
from .comparis import ComparisScraper
from .flatfox import FlatfoxScraper
from .newhome import NewhomeScraper

ALL_SCRAPERS = [
    HomegateScraper,
    ImmoScoutScraper,
    ComparisScraper,
    FlatfoxScraper,
    NewhomeScraper,
]


def run_all_scrapers(profile):
    """Run all scrapers for a profile and return combined results"""
    all_results = []
    for ScraperClass in ALL_SCRAPERS:
        scraper = ScraperClass()
        try:
            results = scraper.search(profile)
            all_results.extend(results)
            print(f"[{scraper.name}] Found {len(results)} properties")
        except Exception as e:
            print(f"[{scraper.name}] Error: {e}")
    return all_results
