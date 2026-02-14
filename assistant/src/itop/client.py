import json
from typing import Any, Dict, Optional, Union

import httpx


class ITopClient:
    def __init__(
        self,
        url: str,
        auth_user: Optional[str] = None,
        auth_pwd: Optional[str] = None,
        auth_token: Optional[str] = None,
        version: str = "1.3",
    ):
        """
        Initialize iTop API client.

        :param url: URL to rest.php (e.g., http://itop/webservices/rest.php)
        :param auth_user: iTop username (for basic auth)
        :param auth_pwd: iTop password (for basic auth)
        :param auth_token: iTop auth token (passed in Auth-Token header)
        :param version: iTop API version
        """
        self.url = url
        self.auth_user = auth_user
        self.auth_pwd = auth_pwd
        self.auth_token = auth_token
        self.version = version

    def _post(self, operation: str, json_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Internal method to perform a POST request to iTop API.
        """
        data = {"version": self.version, "json_data": json.dumps({"operation": operation, **json_data})}

        if self.auth_user:
            data["auth_user"] = self.auth_user
        if self.auth_pwd:
            data["auth_pwd"] = self.auth_pwd

        headers = {}
        if self.auth_token:
            headers["Auth-Token"] = self.auth_token

        response = httpx.post(self.url, data=data, headers=headers)
        response.raise_for_status()

        result = response.json()
        if result.get("code") != 0:
            raise Exception(f"iTop API Error {result.get('code')}: {result.get('message')}")

        return result

    def get_objects(
        self,
        class_name: str,
        key: Union[str, int, Dict[str, Any]],
        output_fields: Optional[list[str]] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Get objects (core/get).

        :param class_name: Class name (e.g., 'UserRequest')
        :param key: OQL query or key array
        :param output_fields: List of field names (default is '*')
        :param limit: Maximum number of objects to return
        :param page: Page number (starting from 1)
        """
        if output_fields:
            fields_str = ",".join(output_fields)
        else:
            fields_str = "*"

        json_data = {"class": class_name, "key": key, "output_fields": fields_str}

        if limit is not None:
            json_data["limit"] = limit
        if page is not None:
            json_data["page"] = page

        return self._post("core/get", json_data)

    def update_object(
        self,
        class_name: str,
        key: Union[str, int, Dict[str, Any]],
        fields: Dict[str, Any],
        comment: str = "",
        output_fields: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """
        Update object (core/update).

        :param class_name: Class name
        :param key: Object key (ID or OQL)
        :param fields: Dictionary of fields to update
        :param comment: Comment for the change
        :param output_fields: List of returned field names
        """
        if output_fields:
            fields_str = ",".join(output_fields)
        else:
            fields_str = "*"

        json_data = {"class": class_name, "key": key, "fields": fields, "comment": comment, "output_fields": fields_str}
        return self._post("core/update", json_data)
