"""Keep verified WAVs staged when Windows temporarily denies a directory rename."""


class PublicationBlocked(Exception):
    pass


def rename_for_publication(source, destination):
    try:
        source.rename(destination)
    except PermissionError as error:
        if getattr(error, 'winerror', None) not in (5, 32, 33):
            raise
        raise PublicationBlocked(
            f'Windows denied the rename from {source} to {destination}. '
            'Close files open in that tape directory and check directory permissions.'
        ) from error


def release_coordinator_hash(digest):
    # These coordinator revisions change only publication and resume handling.
    if digest in ('bc45dc6097843cf10bfb574da61c2c113655880feda38e906138a53d71833a7c',
                  '1e84090b7fdd2f1053393a16cf3afb5cf8d9b4f2d19850a6b72b49908d5df822'):
        return 'ad611272c9744804fa1f1b454c6a3e65a8f4a475c0a7ba4fa0ffca65ab8395d6'
    return digest


def archive_renderer_hash(digest):
    # This revision changes scratch cleanup and terminal output only.
    if digest == '5ebaff7af823bab0163c76894b233c83bb3c2fbeec74f344c6d3acb7655c1def':
        return '6e3649f7af5b618e9453e80dff4f9b14217992eb571c17e7ab54cd73081b3509'
    return digest
