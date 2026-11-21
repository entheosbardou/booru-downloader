"""Independent per-site validation for new and resumed search batches."""
from types import SimpleNamespace


def prepare_sites(db,source_ids,get_source,*,tags=None,rating=''):
    clients,unavailable={},{}
    for source_id in source_ids:
        try:
            client=get_source(db,source_id)
            clients[source_id]=client
            if not client.enabled:
                raise ValueError('disabled')
            if client.api=='unconfigured':
                raise ValueError('needs an adapter under Sites')
            if client.requires_credentials and not client.has_credentials:
                raise ValueError('needs credentials under Sites')
            if tags is not None:
                client.with_rating(tags,rating)
        except (ValueError,OSError) as exc:
            clients.setdefault(source_id,SimpleNamespace(name=f'Site {source_id}'))
            unavailable[source_id]=str(exc) if isinstance(exc,ValueError) else 'Site settings could not be read.'
    return clients,unavailable
