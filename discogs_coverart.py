"""Cover art from Discogs.

Registers alongside Picard's built-in providers (Cover Art Archive and the
rest), so Discogs images are used for releases MusicBrainz has no art for —
which is common for reissues, small labels and regional pressings.

The provider is asynchronous: finding the right Discogs release can take one
or two API calls, so queue_images() returns WAIT and next_in_queue() is
called once the images have been queued (or once it is clear there are
none). Getting that wrong stalls Picard's whole cover art pipeline, so every
exit path below must reach _finish().
"""

from __future__ import annotations

from .discogs_runtime import log, settings
# Picard 3 re-exports both of these from the plugin API module, which is the
# supported surface for plugins; picard.coverart.* is internal and free to
# move between releases.
from picard.plugin3.api import CoverArtImage, CoverArtProvider

from .discogs_client import DiscogsClient
from .discogs_logging import plugin_log
from .discogs_mapping import discogs_release_id_from_mb

# Discogs image `type` values map onto Picard/CAA cover art types.
# Discogs only distinguishes 'primary' from 'secondary'; it does not say
# whether a secondary image is the back, the spine, an inner sleeve or a
# label scan. Typing every secondary as 'back' was wrong — it produced
# back.jpg, back (1).jpg, back (2).jpg for images that are nothing of the
# sort. The first secondary is usually the back cover, so that heuristic
# is kept for exactly one image; the rest are honestly labelled 'other'.
_PRIMARY_TYPE = 'front'
_FIRST_SECONDARY_TYPE = 'back'
_OTHER_SECONDARY_TYPE = 'other'

# How much of a release's artwork to take.
ART_FRONT = 'front'
ART_FRONT_BACK = 'front_back'
ART_ALL = 'all'


class CoverArtProviderDiscogs(CoverArtProvider):
    NAME = 'Discogs'
    TITLE = 'Discogs'

    def enabled(self):
        self.op = plugin_log.new_operation('art')
        scope = settings['cover_art_scope'] or ART_FRONT
        # The base class stops a provider once a front image has been found.
        # That is right when we only want the front, but wrong when the user
        # asked for back/all artwork: an earlier provider supplying a front
        # cover says nothing about the other images, which only Discogs has.
        if scope == ART_FRONT and not super().enabled():
            plugin_log.decision(self.op, 'cover art skipped: another provider already '
                                         'supplied a front image (move Discogs above it '
                                         'in Options > Cover Art to take precedence)')
            log.debug('Discogs Enrichment: cover art skipped, front image already found')
            return False
        if not settings['token']:
            plugin_log.decision(self.op, 'cover art skipped: no Discogs token set')
            log.debug('Discogs Enrichment: cover art skipped, no Discogs token set')
            return False
        if not settings['cover_art']:
            plugin_log.decision(self.op, 'cover art skipped: disabled in plugin settings')
            log.debug('Discogs Enrichment: cover art disabled in plugin settings')
            return False
        plugin_log.event(self.op, f'cover art provider active (scope={scope})')
        log.debug('Discogs Enrichment: cover art provider active for %r', self.album)
        return True

    def queue_images(self):
        token = settings['token']
        client = DiscogsClient(self.album.tagger.webservice, token)
        client.op = getattr(self, 'op', None) or plugin_log.new_operation('art')
        self.op = client.op

        release_id = discogs_release_id_from_mb(self.release)
        if release_id:
            try:
                client.get_release(release_id, self._on_release)
            except Exception:
                log.error('Discogs Enrichment: cover art request failed', exc_info=True)
                return CoverArtProvider.FINISHED
            return CoverArtProvider.WAIT

        # No MusicBrainz->Discogs link, so identify the release the same way
        # the tagging side does rather than guessing.
        barcode = self.metadata.get('barcode') or None
        catalog_number = self.metadata.get('catalognumber') or None
        artist = self.metadata.get('albumartist') or self.metadata.get('artist') or None
        album_title = self.metadata.get('album') or None
        if not any([barcode, (catalog_number and artist), (artist and album_title)]):
            plugin_log.decision(self.op, 'cover art: not enough metadata to find the '
                                         'release on Discogs')
            return CoverArtProvider.FINISHED

        try:
            client.search_release(
                barcode=barcode,
                catalog_number=catalog_number,
                artist=artist,
                release_title=album_title,
                handler=self._on_search,
            )
        except Exception:
            log.error('Discogs Enrichment: cover art search failed', exc_info=True)
            return CoverArtProvider.FINISHED
        return CoverArtProvider.WAIT

    # -- callbacks ---------------------------------------------------------

    def _finish(self):
        """Hand control back to Picard. Must run on every path."""
        try:
            self.next_in_queue()
        except Exception:
            plugin_log.failure(getattr(self, 'op', 'art'),
                               'cover art provider failed to continue')
            log.error('Discogs Enrichment: cover art provider failed to continue',
                      exc_info=True)

    def _on_search(self, summary):
        # If anything here raises, next_in_queue() would never be called and
        # Picard's cover art pipeline would stall for this album, so the
        # follow-up request is issued inside a guard that still finishes.
        try:
            if not summary:
                self._finish()
                return
            token = settings['token']
            client = DiscogsClient(self.album.tagger.webservice, token)
            # Keep the same correlation id so the follow-up request appears
            # under the operation it belongs to, not a fresh one.
            client.op = getattr(self, 'op', None)
            client.get_release(summary.get('resource_url') or summary.get('id'),
                               self._on_release)
        except Exception:
            log.error('Discogs Enrichment: cover art search follow-up failed',
                      exc_info=True)
            self._finish()

    def _on_release(self, release_data):
        try:
            if release_data:
                self._queue_from(release_data)
        except Exception:
            log.error('Discogs Enrichment: error queueing Discogs cover art',
                      exc_info=True)
        self._finish()

    def _queue_from(self, release_data):
        images = release_data.get('images') or []
        if not images:
            plugin_log.decision(self.op, f'cover art: Discogs release '
                                         f'{release_data.get("id")} has no images')
            log.debug('Discogs Enrichment: Discogs release %s has no images',
                      release_data.get('id'))
            return

        # Discogs returns the images array even when a request is NOT
        # authenticated, but with every uri/resource_url set to an empty
        # string (see its API docs and the recurring forum reports). That
        # looks identical to "release has no artwork" unless it is called
        # out, so check for it explicitly before doing anything else.
        if images and not any((im.get('uri') or im.get('resource_url')) for im in images):
            plugin_log.decision(
                self.op,
                f'cover art: release {release_data.get("id")} lists {len(images)} '
                'image(s) but every URL is empty \u2014 Discogs only returns image '
                'URLs to AUTHENTICATED requests, so the token is not being '
                'accepted. Check the token under Options > Plugins > Discogs '
                'Enrichment.')
            log.warning('Discogs Enrichment: Discogs returned %d image(s) with no '
                        'URLs — the API token is missing or rejected', len(images))
            return

        scope = settings['cover_art_scope'] or ART_FRONT
        queued = 0
        seen_secondary = False
        for image in images:
            # Prefer `uri`: it is the public CDN link (i.discogs.com).
            # `resource_url` can point at api.discogs.com, which requires the
            # Authorization header — and Picard's image downloader does not
            # send it, so those URLs fail with 401/403 and no art appears.
            url = image.get('uri') or image.get('resource_url')
            if not url:
                continue
            is_primary = (image.get('type') or '').lower() == 'primary'
            if is_primary:
                kind = _PRIMARY_TYPE
            elif not seen_secondary:
                kind = _FIRST_SECONDARY_TYPE
                seen_secondary = True
            else:
                kind = _OTHER_SECONDARY_TYPE
            if scope == ART_FRONT and kind != _PRIMARY_TYPE:
                continue
            if scope == ART_FRONT_BACK and kind == _OTHER_SECONDARY_TYPE:
                continue
            self.queue_put(CoverArtImage(
                url=url,
                types=[kind],
                comment='Discogs',
            ))
            queued += 1
        plugin_log.event(self.op, f'cover art: queued {queued} of {len(images)} image(s) '
                                  f'from release {release_data.get("id")} (scope={scope})')
        log.debug('Discogs Enrichment: queued %d Discogs image(s) for release %s',
                  queued, release_data.get('id'))
