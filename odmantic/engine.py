import asyncio
from asyncio.tasks import gather
from typing import Dict, List, Optional, Sequence, Type, TypeVar, Union, cast

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic.utils import lenient_issubclass
from pymongo.errors import DuplicateKeyError as PyMongoDuplicateKeyError

from odmantic.exceptions import DuplicatePrimaryKeyError
from odmantic.reference import ODMReference

from .model import Model

ModelType = TypeVar("ModelType", bound=Model)


class AIOEngine:
    def __init__(self, motor_client: AsyncIOMotorClient, db_name: str):
        self.client = motor_client
        self.db_name = db_name
        self.database = motor_client[self.db_name]

    def _get_collection(self, model: Type[ModelType]):
        return self.database[model.__collection__]

    async def find(
        self,
        model: Type[ModelType],
        query: Union[Dict, bool] = {},  # bool: allow using binary operators with mypy
        *,
        limit: int = 0,
        skip: int = 0
    ) -> List[ModelType]:
        if not lenient_issubclass(model, Model):
            raise TypeError("Can only call find with a Model class")

        collection = self._get_collection(model)
        pipeline: List[Dict] = [{"$match": query}]
        if limit > 0:
            pipeline.append({"$limit": limit})
        if skip > 0:
            pipeline.append({"$skip": skip})
        if len(model.__references__) > 0:
            for ref_field_name in model.__references__:
                odm_reference = cast(ODMReference, model.__odm_fields__[ref_field_name])
                pipeline.append(
                    {
                        "$lookup": {
                            "from": odm_reference.model.__collection__,
                            "localField": odm_reference.key_name,
                            "foreignField": "_id",
                            "as": ref_field_name,
                            # FIXME if ref field name is an existing key_name ?
                        }
                    }
                )
                pipeline.append({"$unwind": ref_field_name})
                # TODO handle nested references

        cursor = collection.aggregate(pipeline)
        raw_docs = await cursor.to_list(length=None)
        instances = []
        for raw_doc in raw_docs:
            instance = model.parse_doc(raw_doc)
            instances.append(instance)
        return instances

    async def find_one(
        self,
        model: Type[ModelType],
        query: Union[Dict, bool] = {},  # bool: allow using binary operators w/o plugin
    ) -> Optional[ModelType]:
        if not lenient_issubclass(model, Model):
            raise TypeError("Can only call find_one with a Model class")
        results = await self.find(model, query, limit=1)
        if len(results) == 0:
            return None
        return results[0]

    async def _save(self, instance: ModelType, session) -> None:
        doc = instance.doc()
        save_tasks = []
        for ref_field_name in instance.__references__:
            sub_instance = cast(Model, getattr(instance, ref_field_name))
            save_tasks.append(self._save(sub_instance, session))

        await gather(*save_tasks)

        collection = self._get_collection(type(instance))
        await collection.update_one(
            {"_id": doc["_id"]},
            {"$set": doc},
            upsert=True,
            bypass_document_validation=True,
        )

    async def save(self, instance: ModelType) -> ModelType:
        try:
            async with await self.client.start_session() as s:
                async with s.start_transaction():
                    await self._save(instance, s)
        except PyMongoDuplicateKeyError as e:
            if "_id" in e.details["keyPattern"]:
                raise DuplicatePrimaryKeyError(instance)
            raise
        return instance

    async def save_all(self, instances: Sequence[ModelType]) -> List[ModelType]:
        added_instances = await asyncio.gather(
            *[self.save(instance) for instance in instances]
        )
        return added_instances

    async def delete(self, instance: ModelType) -> int:
        # TODO handle cascade deletion
        collection = self.database[instance.__collection__]
        pk_name = instance.__primary_key__
        result = await collection.delete_many({"_id": getattr(instance, pk_name)})
        return int(result.deleted_count)

    async def count(self, model: Type[ModelType], query: Union[Dict, bool] = {}) -> int:
        if not lenient_issubclass(model, Model):
            raise TypeError("Can only call count with a Model class")
        collection = self.database[model.__collection__]
        count = await collection.count_documents(query)
        return int(count)
