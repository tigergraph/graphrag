import os
import json
import uuid
import logging

from pyTigerGraph import TigerGraphConnection
from common.config import embedding_store_type

from common.py_schemas.schemas import (
    # GraphRAGResponse,
    CreateIngestConfig,
    # LoadingInfo,
    # SupportAIInitConfig,
    # SupportAIMethod,
    # SupportAIQuestion,
)

logger = logging.getLogger(__name__)

def init_supportai(conn: TigerGraphConnection, graphname: str) -> tuple[dict, dict]:
    # need to open the file using the absolute path
    ver = conn.getVer().split(".")

    current_schema = conn.gsql("""USE GRAPH {}\n ls""".format(graphname))

    if "- VERTEX ResolvedEntity" in current_schema:
        schema_res="Schema already exists, skipped"
    else:
        file_path = "common/gsql/supportai/SupportAI_Schema.gsql"
        with open(file_path, "r") as f:
            schema = f.read()
        schema_res = conn.gsql(
            """USE GRAPH {}\n{}\nRUN SCHEMA_CHANGE JOB add_supportai_schema""".format(
                graphname, schema
            )
        )

    if embedding_store_type == "tigergraph":
        if "- embedding(Dimension=" in current_schema:
            schema_res+=" Embeddding schema already exists, skipped"
        else:
            if int(ver[0]) >= 4 and int(ver[1]) >= 2:
                file_path = "common/gsql/supportai/SupportAI_Schema_Native_Vector.gsql"
                with open(file_path, "r") as f:
                    schema = f.read()
                schema_res += " "
                schema_res += conn.gsql(
                    """USE GRAPH {}\n{}\nRUN SCHEMA_CHANGE JOB add_supportai_vector""".format(
                        graphname, schema
                    )
                )

                logger.info(f"Installing GDS library")
                q_res = conn.gsql(
                    """USE GLOBAL\nimport package gds\ninstall function gds.**"""
                )
                logger.info(f"Done installing GDS library with status {q_res}")
            else:
                raise Exception(f"Vector feature is not supported by the current TigerGraph version: {ver}")

    if "- doc_chunk_epoch_processed_index" in current_schema:
        index_res="Index already exists, skipped"
    else:
        file_path = "common/gsql/supportai/SupportAI_IndexCreation.gsql"
        with open(file_path) as f:
            index = f.read()
        index_res = conn.gsql(
            """USE GRAPH {}\n{}\nRUN SCHEMA_CHANGE JOB add_supportai_indexes""".format(
                graphname, index
            )
        )

    supportai_queries = [
        "common/gsql/supportai/Scan_For_Updates.gsql",
        "common/gsql/supportai/Update_Vertices_Processing_Status.gsql",
        "common/gsql/supportai/Selected_Set_Display.gsql",
        "common/gsql/supportai/retrievers/GraphRAG_Hybrid_Search_Display.gsql",
        "common/gsql/supportai/retrievers/GraphRAG_Community_Search_Display.gsql",
        "common/gsql/supportai/retrievers/Chunk_Sibling_Search.gsql",
        "common/gsql/supportai/retrievers/Content_Similarity_Search.gsql",
        "common/gsql/supportai/retrievers/GraphRAG_Hybrid_Search.gsql",
        "common/gsql/supportai/retrievers/GraphRAG_Community_Search.gsql",
    ]

    for filename in supportai_queries:
        logger.info(f"Creating supportai query {filename}")
        with open(f"{filename}", "r") as f:
            q_body = f.read()
        q_name, extension = os.path.splitext(os.path.basename(filename))
        q_res = conn.gsql(
            """USE GRAPH {}\nBEGIN\n{}\nEND\n""".format(
                conn.graphname, q_body
            )
        )
        logger.info(f"Done creating supportai query {q_name} with status {q_res}")

    logger.info(f"Installing supportai queries all together")
    query_res = conn.gsql(
        """USE GRAPH {}\nINSTALL QUERY ALL\n""".format(
            conn.graphname
        )
    )
    logger.info(f"Done installing supportai query all with status {query_res}")

    return schema_res, index_res, query_res


def create_ingest(
    graphname: str,
    ingest_config: CreateIngestConfig,
    conn: TigerGraphConnection,
):
    if ingest_config.file_format.lower() == "json":
        file_path = "common/gsql/supportai/SupportAI_InitialLoadJSON.gsql"

        with open(file_path) as f:
            ingest_template = f.read()
        ingest_template = ingest_template.replace("@uuid@", str(uuid.uuid4().hex))
        doc_id = ingest_config.loader_config.get("doc_id_field", "doc_id")
        doc_text = ingest_config.loader_config.get("content_field", "content")
        doc_type = ingest_config.loader_config.get("doc_type", "")
        ingest_template = ingest_template.replace('"doc_id"', '"{}"'.format(doc_id))
        ingest_template = ingest_template.replace('"content"', '"{}"'.format(doc_text))
        ingest_template = ingest_template.replace('"doc_type"', '"{}"'.format(doc_type))

    if ingest_config.file_format.lower() == "csv":
        file_path = "common/gsql/supportai/SupportAI_InitialLoadCSV.gsql"

        with open(file_path) as f:
            ingest_template = f.read()
        ingest_template = ingest_template.replace("@uuid@", str(uuid.uuid4().hex))
        separator = ingest_config.get("separator", "|")
        header = ingest_config.get("header", "true")
        eol = ingest_config.get("eol", "\n")
        quote = ingest_config.get("quote", "double")
        ingest_template = ingest_template.replace('"|"', '"{}"'.format(separator))
        ingest_template = ingest_template.replace('"true"', '"{}"'.format(header))
        ingest_template = ingest_template.replace('"\\n"', '"{}"'.format(eol))
        ingest_template = ingest_template.replace('"double"', '"{}"'.format(quote))

    file_path = "common/gsql/supportai/SupportAI_DataSourceCreation.gsql"

    with open(file_path) as f:
        data_stream_conn = f.read()

    # assign unique identifier to the data stream connection

    data_stream_conn = data_stream_conn.replace(
        "@source_name@", "SupportAI_" + graphname + "_" + str(uuid.uuid4().hex)
    )

    # check the data source and create the appropriate connection
    res = {"data_source": ingest_config.data_source.lower()}

    if ingest_config.data_source.lower() == "s3":
        data_conn = ingest_config.data_source_config
        if (
            data_conn.get("aws_access_key") is None
            or data_conn.get("aws_secret_key") is None
        ):
            raise Exception("AWS credentials not provided")
        connector = {
            "type": "s3",
            "access.key": data_conn["aws_access_key"],
            "secret.key": data_conn["aws_secret_key"],
        }

        data_stream_conn = data_stream_conn.replace(
            "@source_config@", json.dumps(connector)
        )

    elif ingest_config.data_source.lower() == "azure":
        if ingest_config.data_source_config.get("account_key") is not None:
            connector = {
                "type": "abs",
                "account.key": ingest_config.data_source_config["account_key"],
            }
        elif ingest_config.data_source_config.get("client_id") is not None:
            # verify that the client secret is also provided
            if ingest_config.data_source_config.get("client_secret") is None:
                raise Exception("Client secret not provided")
            # verify that the tenant id is also provided
            if ingest_config.data_source_config.get("tenant_id") is None:
                raise Exception("Tenant id not provided")
            connector = {
                "type": "abs",
                "client.id": ingest_config.data_source_config["client_id"],
                "client.secret": ingest_config.data_source_config["client_secret"],
                "tenant.id": ingest_config.data_source_config["tenant_id"],
            }
        else:
            raise Exception("Azure credentials not provided")
        data_stream_conn = data_stream_conn.replace(
            "@source_config@", json.dumps(connector)
        )
    elif ingest_config.data_source.lower() == "gcs":
        # verify that the correct fields are provided
        if ingest_config.data_source_config.get("project_id") is None:
            raise Exception("Project id not provided")
        if ingest_config.data_source_config.get("private_key_id") is None:
            raise Exception("Private key id not provided")
        if ingest_config.data_source_config.get("private_key") is None:
            raise Exception("Private key not provided")
        if ingest_config.data_source_config.get("client_email") is None:
            raise Exception("Client email not provided")
        connector = {
            "type": "gcs",
            "project_id": ingest_config.data_source_config["project_id"],
            "private_key_id": ingest_config.data_source_config["private_key_id"],
            "private_key": ingest_config.data_source_config["private_key"],
            "client_email": ingest_config.data_source_config["client_email"],
        }
        data_stream_conn = data_stream_conn.replace(
            "@source_config@", json.dumps(connector)
        )
    elif ingest_config.data_source.lower() == "local":
        pass
    else:
        raise Exception("Data source not implemented")

    load_job_created = conn.gsql("USE GRAPH {}\n".format(graphname) + ingest_template)

    res["load_job_id"] = load_job_created.split(":")[1].strip(" [").strip(" ").strip(".").strip("]")
    if ingest_config.data_source_config:
        res["data_path"] = ingest_config.data_source_config.get("data_path", "")

    if ingest_config.data_source.lower() == "local":
        res["data_source_id"] = "DocumentContent"
    else:
        data_source_created = conn.gsql(
            "USE GRAPH {}\n".format(graphname) + data_stream_conn
        )
        res["data_source_id"] = data_source_created.split(":")[1].strip(" [").strip(" ").strip(".").strip("]")

    return res
