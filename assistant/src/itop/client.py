import os


def _create_itop_client():
    from itop_client import Itop

    return Itop(
        url=os.getenv("ITOP_URL", "http://localhost/webservices/rest.php"),
        version="1.3",
        auth_user=os.getenv("ITOP_USER"),
        auth_pwd=os.getenv("ITOP_PWD"),
        auth_token=os.getenv("ITOP_TOKEN"),
    )


itop_client = _create_itop_client()
