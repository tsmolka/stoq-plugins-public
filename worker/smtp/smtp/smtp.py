import os
import re
import time
import pyzmail
import argparse
from tnefparse import TNEF

from stoq.args import StoqArgs
from stoq.filters import StoqBloomFilter
from stoq.plugins import StoqWorkerPlugin
from stoq.scan import get_md5, get_sha1, get_sha256, get_sha512, get_ssdeep, get_magic


class SmtpScan(StoqWorkerPlugin):

    def __init__(self):
        super().__init__()

    def activate(self, stoq):

        self.stoq = stoq

        parser = argparse.ArgumentParser()
        parser = StoqArgs(parser)

        worker_opts = parser.add_argument_group("Plugin Options")
        worker_opts.add_argument("-c", "--attachment_connector",
                                 dest='attachment_connector',
                                 help="Connector plugin to save attachments with")

        options = parser.parse_args(self.stoq.argv[2:])

        super().activate(options=options)

        self.publisher = None

        try:
            # Initialize the IOC extract regex
            self.load_reader('iocregex')
            self.extract_iocs = True
        except AttributeError:
            self.stoq.log.warn("IOC Regex reader plugin is not installed. "
                               " IOC's will not be extracted")
            self.extract_iocs = False

        if self.use_bloom:
            # Initialize bloomfilters
            self.bloomfilters = {}

            for bloomfield in self.bloomfield_list:
                self.bloomfilters[bloomfield] = StoqBloomFilter()
                bloomfield_path = os.path.join(self.bloom_directory,
                                               bloomfield)

                # Ensure path exists
                directory = os.path.dirname(bloomfield_path)
                if not os.path.exists(directory):
                    os.makedirs(directory)

                # Check whether a given filter exists
                if os.path.isfile(bloomfield_path):
                    # Filter already exists, import it
                    self.bloomfilters[bloomfield].import_filter(bloomfield_path)
                else:
                    # Unable to find filter, create brand new one
                    self.bloomfilters[bloomfield].create_filter(bloomfield_path,
                                                                self.bloom_size,
                                                                self.bloom_fp_rate)

                # Set backup schedule at regular intervals
                self.bloomfilters[bloomfield].backup_scheduler(self.bloom_update)

        # Load the attachment connector plugin
        self.load_connector(self.attachment_connector)

        # If we are using a queue to handle attachments, load the plugin now
        # otherwise, load the worker plugins we will be using and set the
        # output connector
        if self.use_queue:
            self.publisher = self.stoq.load_plugin('publisher', 'worker')
        else:
            for worker in self.workers_list:
                self.load_worker(worker)
                self.workers[worker].output_connector = self.output_connector

        return True

    def vortex_metadata(self, vortex_filename):
        try:
            vortex_meta = {}
            vortex_filename_split = vortex_filename.split('-')
            vortex_meta['session_start'] = time.strftime("%d %h %Y %H:%M:%S %Z",
                                                         time.localtime(int(vortex_filename_split[2])))
            vortex_meta['session_end'] = time.strftime("%d %h %Y %H:%M:%S %Z",
                                                       time.localtime(int(vortex_filename_split[3])))

            # We need to parse out the last chunk and turn it into the data we
            # want
            try:
                if vortex_filename_split[6].find('s') == -1:
                    # Looks like this is a client file, no need to parse it.
                    return
                vortex_filename_ips = vortex_filename_split[6].split('s')
            except:
                pass

            vortex_meta['src_ip'] = vortex_filename_ips[0].split(':')[0]
            vortex_meta['dest_ip'] = vortex_filename_ips[1].split(':')[0]

            return vortex_meta

        except:
            return None

    def carve_email(self, payload):
        """
        Carve out an e-mail from a raw SMTP session

        """

        regex = re.compile(r'\r\n^DATA\r\n(.*?)\r\n^\.\r\n', re.M | re.S)
        matches = re.findall(regex, payload)
        if matches:
            return matches
        else:
            # Return as a list so we can iterate
            return [payload]

    def attachment_metadata(self, payload=None, filename=None, puuid=None):
        # Make sure we have a payload, otherwise return None
        if not payload or len(payload) <= 0:
            return None

        attachment_json = {}

        # Generate hashes
        attachment_json['md5'] = get_md5(payload)
        attachment_json['sha1'] = get_sha1(payload)
        attachment_json['sha256'] = get_sha256(payload)
        attachment_json['sha512'] = get_sha512(payload)
        attachment_json['ssdeep'] = get_ssdeep(payload)

        # Get magic type
        attachment_json['magic'] = get_magic(payload)

        # Get size
        attachment_json['size'] = len(payload)

        # Define the filename as provided
        attachment_json['filename'] = filename

        # Make sure we have the parent uuid generated with the original email
        attachment_json['puuid'] = puuid

        # Generate a unique ID
        attachment_json['uuid'] = self.stoq.get_uuid

        return attachment_json

    def handle_attachments(self, payload=None, filename=None, puuid=None):

        attachment_json = self.attachment_metadata(payload=payload,
                                                   filename=filename,
                                                   puuid=puuid)

        if attachment_json:

            if self.attachment_connector:
                conn_res = self.save_payload(payload,
                                             self.attachment_connector)

                if self.attachment_connector == 'file':
                    attachment_json['path'] = conn_res['conn_id']

            if self.publisher:
                if not self.attachment_connector:
                    self.stoq.log.error("No archive connector defined. "
                                        "An archive connector must be "
                                        "before queing may be used.")
                publish_json = attachment_json.copy()
                publish_json['submission_list'] = self.workers_list
                self.publisher.scan(payload=None, **publish_json)

            else:
                for worker in self.workers_list:
                    # Handle the payload with the worker, and get the results
                    # along with the templated results, if there is any
                    self.workers[worker].start(payload, **attachment_json)

            return attachment_json

        else:
            return None

    def scan(self, payload, **kwargs):

        extracted_urls = None
        extracted_ips = None

        # Grab the uuid of so we can pass it off to the attachment
        uuid = kwargs.get('uuid', None)

        payload = self.stoq.force_unicode(payload)
        email_sessions = self.carve_email(payload)

        # Get the appropriate metadata from the vortex filename
        vortex_meta = self.vortex_metadata(kwargs['filename'])

        # Iterate over each e-mail session
        for email_session in email_sessions:
            message_json = {}
            message = pyzmail.message_from_string(email_session)

            if vortex_meta:
                # Setup our primary message json blob
                message_json = vortex_meta
                message_json['vortex_filename'] = kwargs['filename']

            # Create a dict of the headers in the session
            for k, v in list(message.items()):
                curr_header = k.lower()
                if curr_header in message_json:
                    # If the header key already exists, let's join them
                    message_json[curr_header] += "\n{}".format(message.get_decoded_header(k))
                else:
                    message_json[curr_header] = message.get_decoded_header(k)

            # str of concatenated ip_headers
            concat_ips = ""

            # Define which headers we want to extract IP addresses from
            ip_headers = ['src_ip',
                          'dest_ip',
                          'received',
                          'x-orig-ip',
                          'x-originating-ip',
                          'x-remote-ip',
                          'x-sender-ip']

            # concat all of our headers into one string for easy searching
            for ip_header in ip_headers:
                if ip_header in message_json:
                    concat_ips += message_json[ip_header]

            # Extract the e-mail body, to include HTML if available
            if message.text_part is not None:
                message_json['body'] = self.stoq.force_unicode(
                    message.text_part.get_payload())
            else:
                message_json['body'] = ""

            if message.html_part is not None:
                message_json['body_html'] = self.stoq.force_unicode(
                    message.html_part.get_payload())
            else:
                message_json['body_html'] = ""

            # Make this easy, merge both text and html body within e-mail
            # for the purpose of extracting any URIs
            email_body = "{}{}".format(message_json['body'],
                                       message_json['body_html'])

            # Extract and normalize any IP addresses in headers
            if self.extract_iocs:
                extracted_ips = self.readers['iocregex'].read(concat_ips,
                                                              datatype_flag='ipv4')

                # Let's get a unique list of IP addresses from extracted data
                if 'ipv4' in extracted_ips:
                    message_json['ips'] = extracted_ips['ipv4']

                # extract and normalize any URLs found
                extracted_urls = self.readers['iocregex'].read(email_body,
                                                               datatype_flag='url')

                # Extract any URLs that may be in the merged body
                if 'url' in extracted_urls:
                    message_json['urls'] = extracted_urls['url']

            # Handle attachments
            message_json['att'] = []
            for mailpart in message.mailparts:
                try:
                    filename = mailpart.filename
                except TypeError:
                    filename = "None"

                # This is a check for winmail.dat files. If successful,
                # skip_attachment will be True and we will use the
                # results from that instead of winmail.dat file itself.
                skip_attachment = False

                if mailpart.type == "text/plain":
                    message_json['body'] += self.stoq.force_unicode(mailpart.get_payload())
                    skip_attachment = True
                else:

                    if filename == "winmail.dat":
                        tnef_results = TNEF(mailpart.get_payload())

                        # we have data, let's handle it.
                        if tnef_results.attachments:
                            # We have a valid file within winmail.dat,
                            # let's make sure we only handle it here.
                            skip_attachment = True
                            for tnef_attachment in tnef_results.attachments:
                                try:
                                    filename = self.stoq.force_unicode(tnef_attachment.name)
                                except:
                                    filename = "None"

                                try:
                                    attachment_json = self.handle_attachments(payload=tnef_attachment.data,
                                                                              filename=filename,
                                                                              puuid=message_json['uuid'])
                                    if attachment_json:
                                        message_json['att'].append(attachment_json)
                                except:
                                    pass

                # Let's handle the attachment normally
                if not skip_attachment:
                    attachment_json = self.handle_attachments(payload=mailpart.get_payload(),
                                                              filename=filename,
                                                              puuid=uuid)
                    if attachment_json:
                        attachment_json['desc'] = mailpart.part.get('Content-Description')
                        attachment_json['type'] = mailpart.type
                        message_json['att'].append(attachment_json)

        if self.use_bloom:
            # Check bloom filters
            for field_name, field_bloom in self.bloomfilters.items():

                # If the configured field name exists in parsed data...
                if field_name in message_json:

                    # extract the field value and check if it has been seen
                    # before...
                    field_value = message_json[field_name]
                    seen_before = field_bloom.query_filter(
                        field_value, add_missing=True)

                    # Generate JSON entry key for flagging new field values
                    field_flag = "{}_isnew".format(field_name)

                    # if the value has not been seen before...
                    if not seen_before:
                        # flag it as new within JSON
                        message_json[field_flag] = True
                    else:
                        message_json[field_flag] = False

        return message_json
