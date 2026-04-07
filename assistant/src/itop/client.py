def _create_itop_client():
    from config import get_settings
    from itop_client import Itop

    s = get_settings()
    return Itop(
        url=s.itop_url,
        version="1.3",
        auth_user=s.itop_user,
        auth_pwd=s.itop_pwd.get_secret_value() if s.itop_pwd else None,
        auth_token=s.itop_token.get_secret_value() if s.itop_token else None,
    )


itop_client = _create_itop_client()
