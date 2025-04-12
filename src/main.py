import json
import base64
import os
import functions_framework
import requests
import google.cloud.logging
import logging
import google.auth
import google.auth.transport.requests
import googleapiclient.discovery
from ipaddress import ip_address, ip_network

client = google.cloud.logging.Client()
client.setup_logging()

logging.basicConfig(
    format="{asctime} - {levelname} - {message}",
    style="{",
    datefmt="%Y-%m-%d %H:%M",
)

PARENT_ID = os.environ.get("PARENT_GROUP", "Parent group not set in Cloud Run Function environment variable")
OKTA_GROUPS_ONLY = os.environ.get("OKTA_GROUPS_ONLY", "Okta Groups Only boolean not set in Cloud Run Function environment variable")

okta_ip_ranges_url = "https://s3.amazonaws.com/okta-ip-ranges/ip_ranges.json"

def gather_okta_ip_ranges():
    try:
        response = requests.get(okta_ip_ranges_url)
        data = response.json()
        all_ip_ranges = []
        for cell_data in data.values():
            if "ip_ranges" in cell_data:
                all_ip_ranges.extend(cell_data["ip_ranges"])
        return all_ip_ranges
    except requests.exceptions.RequestException as e:
        logging.info("Error fetching Okta IP ranges: {}".format(e))
        return []

def is_okta_ip(requesting_ip, okta_ip_ranges):
    try:
        ip = ip_address(requesting_ip)
        for range_str in okta_ip_ranges:
            network = ip_network(range_str)
            if ip in network:
                logging.info("Found Okta IP {} in range {}".format(requesting_ip, range_str))
                return True
        logging.info("Non-Okta IP: {}".format(requesting_ip))
        return False
    except ValueError:
        logging.info("Invalid IP address format: {}".format(requesting_ip))


@functions_framework.cloud_event
def index(cloud_event):
    try:
        decoded_pubsub_message = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8").strip()
        message = json.loads(decoded_pubsub_message)
        requesting_ip = message['protoPayload']['requestMetadata'].get('callerIp', '')
        membership_id = message['protoPayload']['metadata']['event'][0]['parameter'][0]['value']

        okta_ip_ranges = gather_okta_ip_ranges()
        if not okta_ip_ranges:
            return ("Could not retrieve Okta IP ranges", 500)

        if OKTA_GROUPS_ONLY == 'True':
            if requesting_ip:
                if is_okta_ip(requesting_ip, okta_ip_ranges):
                    add_subgroup(membership_id)
                    return ("Group Event Processed Successfully", 200)
                else:
                    logging.info("Not an Okta IP: {}, OKTA_GROUPS_ONLY set as True, Ignoring {}".format(requesting_ip, membership_id))
                    return ("Not an Okta IP", 200)
            else:
                logging.info("No Requesting IP Found, Skipping Group Add")
                return ("No Requesting IP Found", 200)
        else:
            add_subgroup(membership_id)
            return ("Group Event Processed Successfully", 200)
    except Exception as e:
        logging.info("Error processing cloud event: {}".format(e))
        return ("Error processing cloud event", 500)

def get_credentials():
    scopes = ['https://www.googleapis.com/auth/cloud-identity.groups']
    credentials, project_id = google.auth.default(scopes=scopes)
    request = google.auth.transport.requests.Request()
    credentials.refresh(request)
    return credentials,project_id

def add_subgroup(membership_id):
    credentials,project_id = get_credentials()

    service_name = 'cloudidentity'
    api_version = 'v1'
    service = googleapiclient.discovery.build(
        service_name,
        api_version,
        credentials=credentials,
        cache_discovery=False)

    param = "&groupKey.id=" + PARENT_ID
    lookupGroupNameRequest = service.groups().lookup()
    lookupGroupNameRequest.uri += param
    lookupGroupNameResponse = lookupGroupNameRequest.execute()
    groupName = lookupGroupNameResponse.get("name")

    membership = {
      "preferredMemberKey": {"id": membership_id},
      "roles" : {
        "name" : "MEMBER"
      }
    }    
    
    headers = {
        'Authorization': f'Bearer {credentials.token}',
        'Content-Type': 'application/json',
        'x-goog-user-project': project_id
    }
    try:
        response = service.groups().memberships().create(parent=groupName, body=membership).execute()
        preferredMemberKey = response['response']['preferredMemberKey']['id']
        role = response['response']['roles'][0]['name']
        logging.info("{} added as {} to group {}".format(preferredMemberKey, role, PARENT_ID))
    except Exception as e:
        logging.info("{}".format(e))