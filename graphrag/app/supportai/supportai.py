import os
import json
import uuid
import logging
import boto3
import time
import re
import pyTigerGraph as tg
from pyTigerGraph import TigerGraphConnection

from common.config import embedding_dimension
from common.py_schemas.schemas import (
    # GraphRAGResponse,
    CreateIngestConfig,
    LoadingInfo,
    # SupportAIInitConfig,
    # SupportAIMethod,
    # SupportAIQuestion,
)
from common.utils.text_extractors import TextExtractor
logger = logging.getLogger(__name__)

def init_supportai(conn: TigerGraphConnection, graphname: str) -> tuple[dict, dict]:
    # need to open the file using the absolute path
    ver = conn.getVer().split(".")
    logger.info(f"TigerGraph version: {ver}")

    current_schema = conn.gsql("""USE GRAPH {}\n ls""".format(graphname))

    supportai_queries = [
        "common/gsql/supportai/Scan_For_Updates.gsql",
        "common/gsql/supportai/Update_Vertices_Processing_Status.gsql",
        "common/gsql/supportai/Selected_Set_Display.gsql",
        "common/gsql/supportai/retrievers/GraphRAG_Hybrid_Search_Display.gsql",
        "common/gsql/supportai/retrievers/GraphRAG_Community_Search_Display.gsql",
    ]

    logger.info(f"Checking if schema needs to be created")
    if "- VERTEX ResolvedEntity" in current_schema:
        schema_res="Schema already exists, skipped."
    else:
        file_path = "common/gsql/supportai/SupportAI_Schema.gsql"
        with open(file_path, "r") as f:
            schema = f.read()
        schema_res = conn.gsql(
            """USE GRAPH {}\n{}\nRUN SCHEMA_CHANGE JOB add_supportai_schema""".format(
                graphname, schema
            )
        )
    
    # Add Image vertex schema (for storing images from documents)
    if "- VERTEX Image" in current_schema:
        schema_res += " Image schema already exists, skipped."
    else:
        file_path = "common/gsql/supportai/SupportAI_Schema_Images.gsql"
        with open(file_path, "r") as f:
            image_schema = f.read()
        schema_res += " "
        schema_res += conn.gsql(
            """USE GRAPH {}\n{}\nRUN SCHEMA_CHANGE JOB add_image_support_schema""".format(
                graphname, image_schema
            )
        )

    logger.info(f"Checking if embedding schema needs to be created")
    if "- embedding(Dimension=" in current_schema:
        schema_res+=" Embeddding schema already exists, skipped."
    else:
        if int(ver[0]) >= 4 and int(ver[1]) >= 2:
            file_path = "common/gsql/supportai/SupportAI_Schema_Native_Vector.gsql"
            with open(file_path, "r") as f:
                schema = f.read()
            if embedding_dimension != 1536:
                schema = schema.replace(
                    "dimension=1536",
                    f"dimension={embedding_dimension}",
                )
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

            supportai_queries.extend([
                "common/gsql/supportai/retrievers/Content_Similarity_Vector_Search.gsql",
                "common/gsql/supportai/retrievers/Chunk_Sibling_Vector_Search.gsql",
                "common/gsql/supportai/retrievers/GraphRAG_Community_Vector_Search.gsql",
                "common/gsql/supportai/retrievers/GraphRAG_Hybrid_Vector_Search.gsql",
            ])
        else:
            raise Exception(f"Vector feature is not supported by the current TigerGraph version: {ver}")

    logger.info(f"Checking if index needs to be created")
    if "- doc_chunk_epoch_processed_index" in current_schema:
        index_res="Index already exists, skipped."
    else:
        file_path = "common/gsql/supportai/SupportAI_IndexCreation.gsql"
        with open(file_path) as f:
            index = f.read()
        index_res = conn.gsql(
            """USE GRAPH {}\n{}\nRUN SCHEMA_CHANGE JOB add_supportai_indexes""".format(
                graphname, index
            )
        )

    logger.info(f"Checking if loading job needs to be created")
    if "- CREATE LOADING JOB load_documents_content_json {" not in current_schema:
        supportai_queries.extend([
            "common/gsql/supportai/SupportAI_InitialLoadJSON.gsql",
        ])

    if "- CREATE LOADING JOB load_documents_content_json_with_images {" not in current_schema:
        supportai_queries.extend([
            "common/gsql/supportai/SupportAI_InitialLoadJSON_WithImages.gsql",
        ])

    logger.info(f"Creating supportai queries")
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


def trigger_bedrock_bda(input_uri, output_uri, region, aws_access_key, aws_secret_key, data_source_config={}):
    logger.info(f"Triggering Bedrock Data Automation {region}")
    s3 = boto3.client('s3', region_name=region, aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key)
    bda_client = boto3.client('bedrock-data-automation', region_name=region, aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key)
    bda_runtime_client = boto3.client('bedrock-data-automation-runtime', region_name=region, aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key)

    # Get AWS account ID from STS
    sts_client = boto3.client('sts', region_name=region, aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key)
    account_id = sts_client.get_caller_identity()['Account']

    # Set default configuration values
    # Configure granularity options
    granularity_types = data_source_config.get('granularity', ["DOCUMENT", "PAGE", "ELEMENT", "WORD"])

    # Configure text format options
    text_format_types = data_source_config.get('text_format', ["MARKDOWN"])

    # If project ARN is provided, use it, otherwise create a new project
    project_arn = data_source_config.get('project_arn', None)

    try:
        # there is a bug in AWS bedrock, it does not delete projects properly, so here
        # we generate random project name each time below
        # Delete existing project if it exists
        # existing_projects = bda_client.list_data_automation_projects()["projects"]
        # for project in existing_projects:
        #     if project["projectName"] == "barclays-preprocessing-project":
        #         bda_client.delete_data_automation_project(projectArn=project["projectArn"])
        #         time.sleep(2)
        #         break
        project_stage = "LIVE"
        if not project_arn:
            # Create BDA project
            logger.info(f"Creating BDA project")
            project_name = f"bda-preprocessing-{uuid.uuid4().hex[:6]}"
            project_response = bda_client.create_data_automation_project(
                projectName=project_name,
                projectDescription='Preprocessing multi data formats using bedrock data automation',
                projectStage='DEVELOPMENT',
                standardOutputConfiguration={
                    "document": {
                        "extraction": {
                            "granularity": {"types": granularity_types},
                            "boundingBox": {"state": "ENABLED"}
                        },
                        "generativeField": {"state": "ENABLED"},
                        "outputFormat": {
                            "textFormat": {"types": text_format_types},
                            "additionalFileFormat": {"state": "ENABLED"}
                        }
                    },
                    'image': {
                        'extraction': {
                            'category': {
                                'state': 'ENABLED',
                                'types': [
                                    'TEXT_DETECTION',
                                    'LOGOS',
                                ]
                            },
                            'boundingBox': {'state': 'ENABLED'}
                        },
                        'generativeField': {
                            'state': 'ENABLED',
                            'types': [
                                'IMAGE_SUMMARY'
                            ]
                        }
                    }
                }
            )

            project_arn = project_response['projectArn']
            project_stage = "DEVELOPMENT"
            logger.info(f"Created BDA project {project_name} with ARN {project_arn}")

        # Get file names from S3
        s3_pattern = re.compile(r'^s3://[a-z0-9\.-]{3,63}/.+$')
        if s3_pattern.match(input_uri):
            input_bucket = input_uri[5:].split("/")[0]
            input_prefix = "/".join(input_uri[5:].split("/")[1:])
        elif "//" in input_uri or input_uri.startswith("/") or input_uri.startswith("."):
            raise Exception("Input URI is not in the format of s3://<bucket_name>/<prefix>")
        else:
            input_bucket = input_uri.split("/")[0]
            input_prefix = "/".join(input_uri.split("/")[1:])
            input_uri = "s3://" + input_uri

        if input_prefix and not input_prefix.endswith("/"):
            input_prefix += "/"

        if not s3_pattern.match(output_uri):
            if "//" in output_uri or output_uri.startswith("/") or output_uri.startswith("."):
                raise Exception("Output URI is not in the format of s3://<bucket_name>/<prefix>")
            else:
                output_uri = "s3://" + output_uri


        response = s3.list_objects_v2(Bucket=input_bucket, Prefix=input_prefix)
        objects = response.get('Contents', [])
        logger.info(f"Found {len(objects)} objects in S3 bucket {input_uri} with prefix {input_prefix}")
        job_results = []

        # Launch all BDA jobs first and collect job_arns
        # TODO: Handle quota limit
        for obj in objects:
            time.sleep(2)
            file_key = obj['Key'].removeprefix(input_prefix)
            input_s3_uri = f'{input_uri}/{file_key}'
            output_s3_uri = f'{output_uri}/{file_key}'

            bda_response = bda_runtime_client.invoke_data_automation_async(
                inputConfiguration={
                    's3Uri': input_s3_uri
                },
                outputConfiguration={
                    's3Uri': output_s3_uri
                },
                dataAutomationConfiguration={
                    'dataAutomationProjectArn': project_arn,
                    'stage': project_stage
                },
                dataAutomationProfileArn=f'arn:aws:bedrock:{region}:{account_id}:data-automation-profile/us.data-automation-v1'
            )
            job_arn = bda_response.get('invocationArn')
            logger.info(f"Created BDA job for file {file_key} with job ID {job_arn}")
            job_results.append({
                'file': file_key,
                'jobId': job_arn,
                'status': None
            })

        # Poll all jobs' statuses
        logger.info(f"Waiting for {len(job_results)} BDA jobs to complete")
        max_timeout = 3600 # 1 hour max timeout
        while max_timeout > 0:
            for job in job_results:
                if job['status'] is not None:
                    continue
                job_arn = job['jobId']
                file_key = job['file']
                status_response = bda_runtime_client.get_data_automation_status(invocationArn=job_arn)
                status = status_response.get('status')
                if status in ['Success', 'FAILED', 'STOPPED']:
                    if status not in ['Success']:
                        logger.info(f"ERROR: job not success for {job_arn} on file {file_key} with end status {status}")
                    else:
                        logger.info(f"BDA job {job_arn} on file {file_key} completed successfully.")
                    job['status'] = status
                time.sleep(1)
                max_timeout -= 1
                if max_timeout % 60 == 0:
                    logger.info(f"Waiting for BDA job {job_arn} on file {file_key} to complete")
            if all(job['status'] for job in job_results):
                break
        if max_timeout <= 0:
            logger.error(f"Timeout: {len(job_results)} BDA jobs not completed within 1 hour")
            return {
                'statusCode': 300,
                'error': f"Timeout: {len(job_results)} BDA jobs not completed within 1 hour, need manual check"
            }
        logger.info(f"Accomplished {len(job_results)} BDA jobs")

        return {
            'statusCode': 200,
            'message': f'Processed {len(job_results)} BDA jobs',
            'jobs': job_results
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'error': str(e)
        }

    finally:
        # Clean up: Delete the BDA project after all jobs are completed
        if not data_source_config.get('project_arn', None):
            try:
                logger.info(f"Cleaning up BDA project: {project_arn}")
                delete_response = bda_client.delete_data_automation_project(projectArn=project_arn)
                logger.info(f"Successfully deleted BDA project: {delete_response}")
            except Exception as cleanup_error:
                logger.warning(f"Failed to delete BDA project {project_arn}: {cleanup_error}")
                # Don't fail the entire operation if cleanup fails


def create_ingest(
    graphname: str,
    ingest_config: CreateIngestConfig,
    conn: TigerGraphConnection,
):
    # Check for invalid combination of multi format and non-s3 data source
    if ingest_config.data_source.lower() in ["bda", "server"] and ingest_config.file_format.lower() != "multi":
        logger.warning(f"File format {ingest_config.file_format.lower()} is not supported for data source {ingest_config.data_source.lower()}")
        ingest_config.file_format = "multi"

    res_ingest_config = {"data_source": ingest_config.data_source.lower()}
    res_ingest_config["file_format"] = ingest_config.file_format.lower()

    # Choose loading job template based on source/format
    if ingest_config.file_format.lower() == "multi" and ingest_config.data_source.lower() == "server":
        # Server multi can include images; use the WithImages loading job
        load_job_id = "load_documents_content_json_with_images"
    elif ingest_config.file_format.lower() == "json" or ingest_config.file_format.lower() == "multi":
        load_job_id = "load_documents_content_json"
    elif ingest_config.file_format.lower() == "csv":
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

        logger.debug(f"Ingest template: USE GRAPH {graphname}\nBEGIN\n{ingest_template}\nEND\n")
        load_job_created = conn.gsql(f"USE GRAPH {graphname}\nBEGIN\n{ingest_template}\nEND\n")
        load_job_id = load_job_created.split(":")[1].strip(" [").strip(" ").strip(".").strip("]")
    else:
        raise Exception("File format not implemented")

    res = {"load_job_id": load_job_id}
    logger.info(f"Set load job for ingestion: {load_job_id}")

    if ingest_config.data_source.lower() in ["s3", "azure", "gcs"]:
        # check the data source and create the appropriate connection
        file_path = "common/gsql/supportai/SupportAI_DataSourceCreation.gsql"
        with open(file_path) as f:
            data_stream_conn = f.read()

        # assign unique identifier to the data stream connection
        data_stream_conn = data_stream_conn.replace(
            "@source_name@", "GraphRAG_" + graphname + "_" + str(uuid.uuid4().hex)
        )

        if ingest_config.data_source.lower() == "s3":
            data_conf = ingest_config.data_source_config
            aws_access_key = data_conf.get("aws_access_key", None)
            aws_secret_key = data_conf.get("aws_secret_key", None)

            if aws_access_key is None or aws_secret_key is None:
                raise Exception("AWS credentials not provided")

            connector = {
                "type": "s3",
                "access.key": aws_access_key,
                "secret.key": aws_secret_key,
            }
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
        data_source_created = conn.gsql(
            "USE GRAPH {}\n".format(graphname) + data_stream_conn
        )
        res_ingest_config["data_source_id"] = data_source_created.split(":")[1].strip(" [").strip(" ").strip(".").strip("]")
        res["data_source_id"] = res_ingest_config
    elif ingest_config.data_source.lower() == "bda":
        data_conf = ingest_config.data_source_config
        aws_access_key = data_conf.get("aws_access_key", None)
        aws_secret_key = data_conf.get("aws_secret_key", None)

        if aws_access_key is None or aws_secret_key is None:
            raise Exception("AWS credentials not provided")

        input_bucket = data_conf.get("input_bucket", None)
        output_bucket = data_conf.get("output_bucket", None)
        region_name = data_conf.get("region_name", None)

        if input_bucket is None or output_bucket is None or region_name is None:
            raise Exception("Input bucket, output bucket, or region name not provided")

        res_ingest_config["aws_access_key"] = aws_access_key
        res_ingest_config["aws_secret_key"] = aws_secret_key
        res_ingest_config["input_bucket"] = input_bucket
        res_ingest_config["output_bucket"] = output_bucket
        res_ingest_config["region_name"] = region_name

        try:
            amazon_bda_result = trigger_bedrock_bda(
                input_bucket, output_bucket, region_name, aws_access_key, aws_secret_key, data_conf)
            if amazon_bda_result.get("statusCode") != 200 and amazon_bda_result.get("statusCode") != 300:
                raise Exception(f"Amazon BDA failed: {amazon_bda_result}")
            else:
                logger.info(f"Amazon BDA completed successfully: {amazon_bda_result}")

            res_ingest_config["bda_jobs"] = amazon_bda_result.get("jobs")
            res_ingest_config["data_source_id"] = "DocumentContent"
            res["data_path"] = output_bucket
            # key name to be changed
            res["data_source_id"] = res_ingest_config
        except Exception as e:
            raise Exception(f"Error during Amazon BDA preprocessing: {e}")
    elif ingest_config.data_source.lower() == "server":
        data_path = ingest_config.data_source_config.get("data_path", None)
        if data_path is None:
            raise Exception("Data path not provided for server processing")
        try:
            # Create temp folder BEFORE processing so extractor can save directly
            temp_session_id = str(uuid.uuid4())
            temp_folder = os.path.join("uploads", "ingestion_temp", graphname, temp_session_id)
            
            # Process files and save immediately to temp folder (memory efficient)
            extractor = TextExtractor()
            server_processing_result = extractor.process_folder(
                data_path, 
                graphname=graphname,
                temp_folder=temp_folder  # Extractor saves files as it processes
            )
            if server_processing_result.get("statusCode") != 200:
                raise Exception(f"Server folder processing failed: {server_processing_result}")
            
            doc_count = server_processing_result.get("num_documents", 0)
            logger.info(f"Server folder processing completed: {server_processing_result.get('message')}")

            res_ingest_config["temp_session_id"] = temp_session_id
            res_ingest_config["temp_folder"] = temp_folder
            res_ingest_config["file_count"] = doc_count
            res_ingest_config["data_source_id"] = "DocumentContent"
            # Use a placeholder path to indicate temp storage
            res["data_path"] = "in_temp_storage"
            res["data_source_id"] = res_ingest_config
        except Exception as e:
            raise Exception(f"Error during server folder processing: {e}")
    elif ingest_config.data_source.lower() == "local":
        res["data_source_id"] = "DocumentContent"
    else:
        raise Exception("Data source not implemented")

    if "data_path" not in res and ingest_config.data_source_config:
        res["data_path"] = ingest_config.data_source_config.get("data_path", "")

    return res


def ingest(
    graphname: str,
    loader_info: LoadingInfo,
    conn: TigerGraphConnection,
):
    """
    Execute the data ingestion process by running a loading job.

    Args:
        graphname: Name of the graph
        loader_info: Loading configuration containing job_id, data_source_id, and file_path
        conn: TigerGraph connection instance

    Returns:
        dict: Contains job_name, job_id, and log_location
    """
    if loader_info.file_path is None:
        raise Exception("File path not provided")
    if loader_info.load_job_id is None:
        raise Exception("Load job id not provided")
    if loader_info.data_source_id is None:
        raise Exception("Data source id not provided")

    if isinstance(loader_info.data_source_id, str):
        try:
            res = conn.gsql(
                'USE GRAPH {}\nRUN LOADING JOB -noprint {} USING {}="{}"'.format(
                    graphname,
                    loader_info.load_job_id,
                    "DocumentContent",
                    "$" + loader_info.data_source_id + ":" + loader_info.file_path,
                )
            )
        except Exception as e:
            if (
                "Running the following loading job in background with '-noprint' option:"
                in str(e)
            ):
                res = str(e)
            else:
                raise e

        log_section = res.split(
            "Running the following loading job in background with '-noprint' option:"
        )[1]
        # Try to extract 'Job name' or 'Log directory'
        if "Job name: " in log_section:
            log_location = log_section.split("Job name: ")[1].split("\n")[0]
        else:
            log_location = log_section.split("Log directory: ")[1].split("\n")[0]
        return {
            "job_name": loader_info.load_job_id,
            "job_id": log_section.split("Jobid: ")[1].split("\n")[0],
            "log_location": log_location,
        }
    elif isinstance(loader_info.data_source_id, dict):
        ingest_config = loader_info.data_source_id
        if ingest_config.get("data_source") == "bda":
            aws_access_key = ingest_config.get("aws_access_key", None)
            aws_secret_key = ingest_config.get("aws_secret_key", None)
            region_name = ingest_config.get("region_name", None)

            if aws_access_key is None or aws_secret_key is None or region_name is None:
                raise Exception("AWS credentials, secret key, or region name not provided")

            # data_path is in the format of s3://<bucket_name>/<prefix>
            s3_pattern = re.compile(r'^s3://[a-z0-9\.-]{3,63}/.+$')
            if s3_pattern.match(loader_info.file_path):
                s3_bucket = loader_info.file_path[5:].split("/")[0]
                s3_prefix = "/".join(loader_info.file_path[5:].split("/")[1:])
            elif "//" in loader_info.file_path or loader_info.file_path.startswith("/") or loader_info.file_path.startswith("."):
                raise Exception("File path is not in the format of s3://<bucket_name>/<prefix>")
            else:
                s3_bucket = loader_info.file_path.split("/")[0]
                s3_prefix = "/".join(loader_info.file_path.split("/")[1:])

            if s3_prefix and not s3_prefix.endswith("/"):
                s3_prefix += "/"

            # --- Begin: S3 markdown extraction and TigerGraph loading ---
            # Possiblely we can download the files locally and then process them with next conn.runDocumentIngest() after it supports folder
            logger.info(f"Starting S3 markdown extraction and TigerGraph loading...")

            try:
                data_source_id = ingest_config.get("data_source_id", "DocumentContent")
                if ingest_config.get("bda_jobs"):
                    job_uids = [job.get("jobId").split("/")[-1] for job in ingest_config.get("bda_jobs")]
                else:
                    job_uids = []

                s3 = boto3.client('s3', region_name=region_name, aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key)
                paginator = s3.get_paginator('list_objects_v2')
                pages = paginator.paginate(Bucket=s3_bucket, Prefix=s3_prefix)
                processed_files = []

                for page in pages:
                    if 'Contents' not in page:
                        continue
                    for obj in page['Contents']:
                        key = obj['Key']
                        if key.endswith('/0/standard_output/0/result.md'):
                            path_parts = key.split('/')
                            if len(path_parts) >= 7:
                                file_name = path_parts[-6]
                                file_uuid = path_parts[-5]
                                if job_uids and file_uuid not in job_uids:
                                    continue

                                response = s3.get_object(Bucket=s3_bucket, Key=key)
                                content = response['Body'].read().decode('utf-8')
                                base_path = "/".join(path_parts[:-1]) + "/assets"

                                # Find and log image matches before replacement
                                content = re.sub(r'!\[([^\]]*)\]\(\./([^)]+)\)', lambda m: f'![{m.group(1)}](s3://{s3_bucket}/{base_path}/{m.group(2)})', content)
                                doc_id = f"{file_name}"
                                # Prepare API payload
                                payload = json.dumps({"doc_id": doc_id, "doc_type": "markdown", "content": content})

                                #CALL API
                                conn.runLoadingJobWithData(payload, data_source_id, loader_info.load_job_id)
                                processed_files.append({
                                    'file_path': key,
                                    'doc_id': doc_id,
                                })
                                # Loading into TigerGraph is now handled elsewhere.
                                logger.info(f"Data uploading done for file: {key} with doc_id: {doc_id}")
                logger.info(f"Processed {len(processed_files)} markdown files from S3 and loaded into TigerGraph.")
                # --- End: S3 markdown extraction and TigerGraph loading ---
            except Exception as e:
                raise Exception(f"Error during S3 markdown extraction and TigerGraph loading: {e}")

            return {
                "job_name": loader_info.load_job_id,
                "summary": processed_files
            }
        elif ingest_config.get("data_source") == "server":
            try:
                data_source_id = ingest_config.get("data_source_id", "DocumentContent")
                
                # Read from temporary folder's JSONL file
                temp_folder = ingest_config.get("temp_folder")
                if not temp_folder or not os.path.exists(temp_folder):
                    raise Exception(f"Temporary folder not found: {temp_folder}")
                
                # Read the entire JSONL file as a string
                jsonl_file = os.path.join(temp_folder, "processed_documents.jsonl")
                if not os.path.exists(jsonl_file):
                    raise Exception(f"JSONL file not found: {jsonl_file}")
                
                logger.info(f"Reading JSONL file: {jsonl_file}")
                
                # Read entire JSONL content
                with open(jsonl_file, 'r', encoding='utf-8') as f:
                    jsonl_content = f.read()
                
                # Load all documents in one call - runLoadingJobWithData supports JSONL format
                conn.runLoadingJobWithData(jsonl_content, data_source_id, loader_info.load_job_id)
                
                # Count documents for reporting
                doc_count = sum(1 for line in jsonl_content.strip().split('\n') if line.strip())
                logger.info(f"Successfully ingested {doc_count} documents from JSONL")
                
                # Clean up temp folder after successful ingestion
                try:
                    import shutil
                    shutil.rmtree(temp_folder)
                    logger.info(f"Cleaned up temporary folder: {temp_folder}")
                except Exception as cleanup_error:
                    logger.warning(f"Failed to cleanup temp folder {temp_folder}: {cleanup_error}")
                    
            except Exception as e:
                raise Exception(f"Error during server markdown extraction and TigerGraph loading: {e}")
            return {
                "job_name": loader_info.load_job_id,
                "summary": f"Successfully ingested {doc_count} documents from JSONL",
                "document_count": doc_count
            }

        else:
            raise Exception("Data source and file format combination not implemented")
    else:
        raise Exception("Data source id is not a string or dict")
