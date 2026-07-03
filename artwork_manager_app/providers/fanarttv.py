import os


class FanartTVProvider:
    name = 'fanart.tv'

    def __init__(self):
        self.key = os.environ.get('FANARTTV_API_KEY', '')

    def get_candidates(self, album_info, max_candidates=5, log=None):
        if not self.key:
            if log:
                log('  fanart.tv skipped: set FANARTTV_API_KEY to enable this fallback.')
            return []
        if log:
            log('  fanart.tv fallback needs release-group lookup support; kept as a configured extension point.')
        return []
