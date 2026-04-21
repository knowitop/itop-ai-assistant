from typing import Any, Dict, List, Optional, Union

from .exceptions import ItopError
from .parallel import tmap


class Schema:
    def __init__(self, itop: Any, name: str):
        self.itop = itop
        self.name = name

    def to_oql(self, query: Dict[str, Any]) -> str:
        oql = f"SELECT {self.name}"
        if query:

            def _clause(k: str, v: Any) -> str:
                if isinstance(v, tuple):
                    op, val = v
                else:
                    op, val = "LIKE", v
                val = str(val)
                if val.startswith(":"):
                    return f"{k} {op} {val}"
                escaped = val.replace("\\", "\\\\").replace('"', '\\"')
                return f'{k} {op} "{escaped}"'

            oql += " WHERE " + " AND ".join([_clause(k, v) for k, v in query.items()])
        return oql

    def __make_key(self, query: Dict[str, Any]) -> str:
        if len(query) == 1 and "id" in query and str(query["id"]).isdigit():
            return str(query["id"])
        return self.to_oql(query)

    async def find(
        self,
        query: Optional[Dict[str, Any] | str] = None,
        projection: Optional[List[str]] = None,
        limit: str = "0",
        page: str = "1",
    ) -> Any:
        query = query or {}
        if not isinstance(query, dict) and (not isinstance(query, str) or not query.startswith("SELECT")):
            raise TypeError("Query must be a dict or a string in OQL format")
        projection = projection or []
        if not isinstance(projection, list):
            raise TypeError("Projection must be a list")

        data = {
            "operation": "core/get",
            "comment": f"Get {self.name}",
            "class": self.name,
            "key": query if isinstance(query, str) else self.__make_key(query),
            "output_fields": "*+",
            "limit": limit,
            "page": page,
        }

        response = await self.itop.request(data)

        output: list = (
            [{k: v for k, v in obj.items() if k in projection} for obj in response] if projection else response
        )

        return output if isinstance(output, list) else []

    async def find_one(
        self,
        query: Optional[Dict[str, Any] | str] = None,
        projection: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        results = await self.find(query, projection, limit="1")
        return results[0] if results else None

    async def find_related(
        self,
        query: Optional[Dict[str, Any] | str] = None,
        relation: str = "impacts",
        depth: int = 20,
        direction: str = "down",
    ) -> List[Dict]:
        query = query or {}
        if not isinstance(query, dict) and (not isinstance(query, str) or not query.startswith("SELECT")):
            raise TypeError("Query must be a dict or a string in OQL format")

        data = {
            "operation": "core/get_related",
            "class": self.name,
            "key": query if isinstance(query, str) else self.__make_key(query),
            "relation": relation,
            "depth": depth,
            "direction": direction,
        }

        response = await self.itop.request(data, raw_response=True)
        return [{**obj["fields"], "id": obj["key"], "class": obj["class"]} for obj in response.values()]

    async def insert(self, objs: Union[Dict, List[Dict]], workers: int = 10) -> List[Dict]:
        if not isinstance(objs, (dict, list)):
            raise TypeError("objs must be a dict or list of dicts")
        objs = objs if isinstance(objs, list) else [objs]

        # Strip leading underscore from field names, drop empty values
        objs = [{(k[1:] if k.startswith("_") else k): v for k, v in obj.items() if v} for obj in objs]

        if self.itop.data_model:
            objs = [await self.lookup(obj) for obj in objs]

        async def do_insert(obj: Dict) -> List:
            return await self.itop.request(
                {
                    "operation": "core/create",
                    "comment": f"Create {self.name}",
                    "class": self.name,
                    "output_fields": "*",
                    "fields": obj,
                }
            )

        results = await tmap(do_insert, objs, workers=workers)
        return [item for result in results for item in result]

    async def update(
        self,
        query: Dict[str, Any] | str,
        update: Dict[str, Any],
        upsert: bool = False,
        multi: bool = False,
    ) -> Any:
        query = query or {}
        if not isinstance(query, dict) and (not isinstance(query, str) or not query.startswith("SELECT")):
            raise TypeError("Query must be a dict or a string in OQL format")
        update = update or {}
        if not isinstance(update, dict):
            raise TypeError("Update must be a dict")

        if self.itop.data_model:
            update = await self.lookup(update)

        data = {
            "operation": "core/update",
            "comment": f"Update {self.name}",
            "class": self.name,
            "output_fields": "*",
            "fields": update,
            "key": query if isinstance(query, str) else self.__make_key(query),
        }

        try:
            return await self.itop.request(data)
        except ItopError as e:
            if "Several items" in str(e):
                if multi:
                    objs = await self.find(query)
                    results = await tmap(lambda obj: self.update(obj, update, upsert, multi), objs, workers=10)
                    output: Any = [item for result in results for item in result]
                    if isinstance(output, list) and len(output) == 1:
                        output = output[0]
                    if isinstance(output, dict) and len(output) == 1:
                        _, output = list(output.items())[0]
                    return output
                raise
            if "No item found for query" in str(e):
                if upsert and isinstance(query, dict):
                    return await self.insert({**query, **update})
                return {}
            raise

    async def remove(self, query: Dict[str, Any] | str) -> Any:
        query = query or {}
        if not isinstance(query, dict) and (not isinstance(query, str) or not query.startswith("SELECT")):
            raise TypeError("Query must be a dict or a string in OQL format")

        output = await self.itop.request(
            {
                "operation": "core/delete",
                "comment": f"Delete {self.name}",
                "class": self.name,
                "key": query if isinstance(query, str) else self.__make_key(query),
            }
        )

        if isinstance(output, list) and len(output) == 1:
            output = output[0]
        if isinstance(output, dict) and len(output) == 1:
            _, output = list(output.items())[0]

        return output

    async def sync(
        self,
        objs: Union[Dict, List[Dict]],
        keys: Optional[List[str]] = None,
        workers: int = 10,
    ) -> Any:
        keys = keys or ["name"]
        if not isinstance(objs, list):
            objs = [objs]

        async def step(obj: Dict) -> Any:
            query = {field: obj[field] for field in obj if field in keys}
            return await self.update(query, obj, upsert=True, multi=False)

        results = await tmap(step, objs, workers=workers)
        output: Any = [item for result in results if result for item in result]

        if isinstance(output, list) and len(output) == 1:
            output = output[0]
        if isinstance(output, dict) and len(output) == 1:
            _, output = list(output.items())[0]

        return output

    async def apply_stimulus(
        self,
        query: Dict[str, Any] | str,
        stimulus_data: Dict[str, Any],
        stimulus: str = "env_assign",
    ) -> Any:
        query = query or {}
        if not isinstance(query, dict) and (not isinstance(query, str) or not query.startswith("SELECT")):
            raise TypeError("Query must be a dict or a string in OQL format")
        stimulus_data = stimulus_data or {}
        if not isinstance(stimulus_data, dict):
            raise TypeError("Stimulus data must be a dict")

        if self.itop.data_model:
            stimulus_data = await self.lookup(stimulus_data)

        return await self.itop.request(
            {
                "operation": "core/apply_stimulus",
                "comment": f"Apply Stimulus {self.name}",
                "class": self.name,
                "output_fields": "*",
                "fields": stimulus_data,
                "stimulus": stimulus,
                "key": query if isinstance(query, str) else self.__make_key(query),
            }
        )

    async def lookup(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve external field names to external keys using the data model."""
        obj = dict(obj)

        schema_lookups = self.itop.data_model.lookupExternalField(self.name)
        for field in [f for f in obj if f in schema_lookups]:
            old_value = obj[field]
            external_key, lookup_class, lookup_field = schema_lookups[field]
            if old_value:
                row = await self.itop.schema(lookup_class).find_one({lookup_field: old_value}, ["id"])
                if row is None:
                    raise ValueError(
                        f'Lookup field error: field "{field}", value "{old_value}", '
                        f'key "{external_key}", schema "{self.name}" → '
                        f'field "{lookup_field}" on schema "{lookup_class}"'
                    )
                obj[external_key] = row["id"]
            else:
                obj[external_key] = None
            del obj[field]

        schema_external_keys = {v[0] for v in schema_lookups.values()}
        for field in [f for f in obj if f in schema_external_keys]:
            if not obj[field]:
                del obj[field]

        schema_linked_sets = self.itop.data_model.lookupLinkedSet(self.name)
        for field in [f for f in obj if f in schema_linked_sets]:
            linked_class, _ext_key_to_me, _ext_key_to_remote = schema_linked_sets[field]
            linked_sets_lookups = self.itop.data_model.lookupExternalField(linked_class)
            for i, child_obj in enumerate(obj[field]):
                for child_field in [f for f in child_obj if f in linked_sets_lookups]:
                    old_value = child_obj[child_field]
                    external_key, lookup_class, lookup_field = linked_sets_lookups[child_field]
                    if old_value:
                        row = await self.itop.schema(lookup_class).find_one({lookup_field: old_value}, ["id"])
                        if row is None:
                            raise ValueError(
                                f'Lookup field error: field "{child_field}", value "{old_value}", '
                                f'key "{external_key}", schema "{self.name}" → '
                                f'field "{lookup_field}" on schema "{lookup_class}"'
                            )
                        obj[field][i][external_key] = row["id"]
                    else:
                        obj[field][i][external_key] = None
                    del obj[field][i][child_field]

        return obj
