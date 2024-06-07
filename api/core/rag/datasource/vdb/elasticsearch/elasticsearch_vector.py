from typing import Any

import requests
from elasticsearch import Elasticsearch
from pydantic import BaseModel, root_validator

from core.rag.datasource.vdb.vector_base import BaseVector
from core.rag.models.document import Document


class ElasticSearchConfig(BaseModel):
    host: str
    port: str
    api_key_id: str
    api_key: str

    @root_validator()
    def validate_config(cls, values: dict) -> dict:
        if not values['host']:
            raise ValueError("config HOST is required")
        if not values['port']:
            raise ValueError("config PORT is required")
        if not values['api_key_id']:
            raise ValueError("config API_KEY_ID is required")
        if not values['api_key']:
            raise ValueError("config API_KEY is required")
        return values


class ElasticSearchVector(BaseVector):
    def __init__(self, index_name: str, config: ElasticSearchConfig, attributes: list):
        super().__init__(index_name.lower())
        self._client = self._init_client(config)
        self._attributes = attributes

    def _init_client(self, config: ElasticSearchConfig) -> Elasticsearch:
        try:
            client = Elasticsearch(
                hosts=f'{config.host}:{config.port}',
                api_key=(config.api_key_id, config.api_key),
                request_timeout=300,
                retry_on_timeout=True,
                max_retries=5,
            )
        except requests.exceptions.ConnectionError:
            raise ConnectionError("Vector database connection error")

        return client

    def get_type(self) -> str:
        return 'elasticsearch'

    def add_texts(self, documents: list[Document], embeddings: list[list[float]], **kwargs):
        uuids = self._get_uuids(documents)
        texts = [d.page_content for d in documents]
        metadatas = [d.metadata for d in documents]

        if not self._client.indices.exists(index=self._collection_name):
            dim = len(embeddings[0])
            mapping = {
                "properties": {
                    "text": {
                        "type": "text"
                    },
                    "vector": {
                        "type": "dense_vector",
                        "index": True,
                        "dims": dim,
                        "similarity": "l2_norm"
                    },
                }
            }
            self._client.indices.create(index=self._collection_name, mappings=mapping)

        added_ids = []
        for i, text in enumerate(texts):
            self._client.index(index=self._collection_name,
                               id=uuids[i],
                               document={
                                   "text": text,
                                   "vector": embeddings[i] if embeddings[i] else None,
                                   "metadata": metadatas[i] if metadatas[i] else {},
                               })
            added_ids.append(uuids[i])

        self._client.indices.refresh(index=self._collection_name)
        return uuids

    def text_exists(self, id: str) -> bool:
        return self._client.exists(index=self._collection_name, id=id).__bool__()

    def delete_by_ids(self, ids: list[str]) -> None:
        for id in ids:
            self._client.delete(index=self._collection_name, id=id)

    def delete_by_metadata_field(self, key: str, value: str) -> None:
        query_str = {
            'query': {
                'match': {
                    f'metadata.{key}': f'{value}'
                }
            }
        }
        results = self._client.search(index=self._collection_name, body=query_str)
        ids = [hit['_id'] for hit in results['hits']['hits']]
        if ids:
            self.delete_by_ids(ids)

    def delete(self) -> None:
        self._client.indices.delete(index=self._collection_name)

    def search_by_vector(self, query_vector: list[float], **kwargs: Any) -> list[Document]:
        query_str = {
            "query": {
                "script_score": {
                    "query": {
                        "match_all": {}
                    },
                    "script": {
                        "source": "cosineSimilarity(params.query_vector, 'vector') + 1.0",
                        "params": {
                            "query_vector": query_vector
                        }
                    }
                }
            }
        }

        results = self._client.search(index=self._collection_name, body=query_str)

        docs_and_scores = []
        for hit in results['hits']['hits']:
            docs_and_scores.append(
                (Document(page_content=hit['_source']['text'], metadata=hit['_source']['metadata']), hit['_score']))

        docs = []
        for doc, score in docs_and_scores:
            score_threshold = kwargs.get("score_threshold", .0) if kwargs.get('score_threshold', .0) else 0.0
            if score > score_threshold:
                doc.metadata['score'] = score
            docs.append(doc)
        return docs

    def search_by_full_text(self, query: str, **kwargs: Any) -> list[Document]:
        query_str = {
            "match": {
                "text": query
            }
        }
        results = self._client.search(index=self._collection_name, query=query_str)
        docs = []
        for hit in results['hits']['hits']:
            docs.append(Document(page_content=hit['_source']['text'], metadata=hit['_source']['metadata']))

        return docs

    def create(self, texts: list[Document], embeddings: list[list[float]], **kwargs):
        return self.add_texts(texts, embeddings, **kwargs)