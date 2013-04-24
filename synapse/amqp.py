import time
import pika
import socket

from Queue import Empty
from ssl import CERT_REQUIRED
from datetime import datetime, timedelta

from pika.adapters import SelectConnection
from pika.adapters.select_connection import SelectPoller
from pika import PlainCredentials

from synapse.logger import logger
from synapse.task import Task


class ExternalCredentials(PlainCredentials):
    """ The PlainCredential class is extended to work with external rabbitmq
    auth mechanism. Here, the rabbitmq-auth-mechanism-ssl plugin than can be
    found here http://www.rabbitmq.com/plugins.html#rabbitmq_auth_mechanism_ssl

    Rabbitmq's configuration must be adapted as follow:
    [
    {rabbit, [
     {auth_mechanisms, ['EXTERNAL', 'PLAIN']},
     {ssl_listeners, [5671]},
     {ssl_options, [{cacertfile,"/etc/rabbitmq/testca/cacert.pem"},
                    {certfile,"/etc/rabbitmq/server/cert.pem"},
                    {keyfile,"/etc/rabbitmq/server/key.pem"},
                    {verify,verify_peer},
                    {fail_if_no_peer_cert,true}]}
    ]}
    ].
    """
    TYPE = 'EXTERNAL'

    def __init__(self):
        self.erase_on_connect = False

    def response_for(self, start):

        if ExternalCredentials.TYPE not in start.mechanisms.split():
            return None, None
        return ExternalCredentials.TYPE, ""

    def erase_credentials(self):
        pass


# As mentioned in pika's PlainCredentials class, we need to append the new
# authentication mechanism to VALID_TYPES
pika.credentials.VALID_TYPES.append(ExternalCredentials)


@logger
class Amqp(object):
    def __init__(self, conf, pq=None, tq=None):
        # RabbitMQ general options
        self.cacertfile = conf['cacertfile']
        self.certfile = conf['certfile']
        self.exchange = conf['exchange']
        self.status_exchange = conf['status_exchange']
        self.fail_if_no_peer_cert = conf['fail_if_no_peer_cert']
        self.heartbeat = conf['heartbeat']
        self.host = conf['host']
        self.keyfile = conf['keyfile']
        self.password = conf['password']
        self.port = conf['port']
        self.ssl_port = conf['ssl_port']
        self.queue = conf['uuid']
        self.retry_timeout = conf['retry_timeout']
        self.ssl_auth = conf['ssl_auth']
        self.use_ssl = conf['use_ssl']
        self.username = conf['username']
        self.vhost = conf['vhost']
        self.redelivery_timeout = conf['redelivery_timeout']

        # Connection and channel initialization
        self._connection = None
        self._consume_channel = None
        self._consume_channel_number = None
        self._publish_channel = None
        self._publish_channel_number = None
        self._message_number = 0
        self._deliveries = {}

        self._closing = False

        self.pq = pq
        self.tq = tq
        self._processing = False

        # Plain credentials
        credentials = PlainCredentials(self.username, self.password)
        pika_options = {'host': self.host,
                        'port': self.port,
                        'virtual_host': self.vhost,
                        'credentials': credentials}

        # SSL options
        if self.use_ssl:
            pika_options['ssl'] = True
            pika_options['port'] = self.ssl_port
            if self.ssl_auth:
                pika_options['credentials'] = ExternalCredentials()
                pika_options['ssl_options'] = {
                    'ca_certs': self.cacertfile,
                    'certfile': self.certfile,
                    'keyfile': self.keyfile,
                    'cert_reqs': CERT_REQUIRED
                }

        if self.heartbeat:
            pika_options['heartbeat'] = self.heartbeat

        self.parameters = None

        try:
            self.parameters = pika.ConnectionParameters(**pika_options)
        except TypeError as err:
            self.logger.debug(err)
            # Let's be compatible with original pika version (no integer for
            # heartbeats and no ssl.
            self.logger.warning("Wrong pika lib version, won't use ssl.")
            pika_options['heartbeat'] = True
            if self.use_ssl:
                self.use_ssl = False
                pika_options['port'] = self.port
                del pika_options['ssl']
                if self.ssl_auth:
                    self.ssl_auth = False
                    del pika_options['ssl_options']

            self.parameters = pika.ConnectionParameters(**pika_options)

        self.print_config()

    def run(self):
        self._connection = self.connect()
        self._message_number = 0
        self._connection.ioloop.start()

    def connect(self):
        SelectPoller.TIMEOUT = .1
        return SelectConnection(self.parameters, self.on_connection_open)

    def print_config(self):
        to_print = ["Port: %s" % self.port,
                    "Queue: %s" % self.queue,
                    "Exchange: %s" % self.exchange,
                    "Heartbeat: %s" % self.heartbeat,
                    "Host: %s" % self.host,
                    "Use_ssl: %s" % self.use_ssl,
                    "Ssl_auth: %s" % self.ssl_auth,
                    "Vhost: %s" % self.vhost,
                    "Redelivery_timeout: %s" % self.redelivery_timeout]
        to_print.sort()
        to_print.insert(0, "##################################")
        to_print.insert(1, "[AMQP-CONFIGURATION]")
        to_print.append("##################################")

        for info in to_print:
            self.logger.info(info)

    def stop(self):
        self.logger.debug("[AMQP] Invoked stop.")
        self._closing = True
        if self._connection:
            try:
                self._connection.close()
                self._connection.ioloop.start()
            except Exception as err:
                self.logger.error(err)
        self.logger.info("[AMQP] Stopped.")

    def on_connection_open(self, connection):
        self.logger.info("[AMQP] Connected to %s." % self.host)
        self.add_on_connection_close_callback()
        self.open_consume_channel()
        self.open_publish_channel()

    def add_on_connection_close_callback(self):
        self._connection.add_on_close_callback(self.on_connection_closed)

    def on_connection_closed(self, frame):
        self._consume_channel = None
        self._publish_channel = None
        if self._closing:
            self._connection.ioloop.stop()

    ##########################
    # Consume channel handling
    ##########################
    def open_consume_channel(self):
        self.logger.debug("Opening consume channel.")
        self._connection.channel(self.on_consume_channel_open)

    def on_consume_channel_open(self, channel):
        self._consume_channel_number = channel.channel_number
        self.logger.debug("Consume channel #%d successfully opened." %
                          channel.channel_number)
        self._consume_channel = channel
        self.add_on_consume_channel_close_callback()
        self.setup_consume()

    def add_on_consume_channel_close_callback(self):
        self._consume_channel.add_on_close_callback(
            self.on_consume_channel_close)

    def on_consume_channel_close(self, code, text):
        self.logger.debug("Consume channel closed [%d - %s]." % (code, text))
        if code == 320:
            raise socket.error
        else:
            self._connection.add_timeout(3, self.open_consume_channel)

    ##########################
    # Publish channel handling
    ##########################
    def open_publish_channel(self):
        self.logger.debug("Opening publish channel.")
        self._connection.channel(self.on_publish_channel_open)

    def on_publish_channel_open(self, channel):
        self._publish_channel_number = channel.channel_number
        self.logger.debug("Publish channel #%d successfully opened." %
                          channel.channel_number)
        self._publish_channel = channel
        self.add_on_publish_channel_close_callback()
        self.setup_publish()

    def add_on_publish_channel_close_callback(self):
        self._publish_channel.add_on_close_callback(
            self.on_publish_channel_close)

    def on_publish_channel_close(self, code, text):
        self.logger.debug("Publish channel closed [%d - %s]." % (code, text))
        if code == 320:
            raise socket.error
        else:
            self._connection.add_timeout(3, self.open_publish_channel)

    ##########################
    # Consuming
    ##########################
    def setup_consume(self):
        self._consume_channel.callbacks.add(
            self._consume_channel.channel_number,
            pika.spec.Basic.GetEmpty,
            self.on_get_empty,
            one_shot=False)
        self.start_getting()

    def start_getting(self):
        if self._processing is False and self._consume_channel is not None:
            self._consumer_tag = self._consume_channel.basic_get(
                callback=self.handle_delivery, queue=self.queue)

    def on_get_empty(self, frame):
        self.next_get()

    def next_get(self):
        self._connection.add_timeout(.1, self.start_getting)

    def handle_delivery(self, channel, method_frame, header_frame, body):
        self._processing = True
        self.logger.debug("[AMQP-RECEIVE] #%s: %s" %
                          (method_frame.delivery_tag, body))
        self._consume_channel.basic_ack(delivery_tag=method_frame.delivery_tag)
        self.logger.debug("[AMQP-ACK] Received message #%s acked" %
                          method_frame.delivery_tag)
        try:
            task = Task(vars(header_frame), body)
            if not method_frame.redelivered:
                self.tq.put(task)
            else:
                self._processing = False
                self.logger.warning("Message redelivered. Won't process.")
        except ValueError as err:
            self._processing = False
            self.logger.error(err)

    ##########################
    # Publishing
    ##########################
    def setup_publish(self):
        self.start_publishing()

    def start_publishing(self):
        self._publish_channel.confirm_delivery(
            callback=self.on_confirm_delivery)
        self._connection.add_timeout(1, self._publisher)
        self._connection.add_timeout(1, self._check_redeliveries)

    def on_confirm_delivery(self, tag):
        self.logger.debug("[AMQP-DELIVERED] #%s" % tag.method.delivery_tag)
        if tag.method.delivery_tag in self._deliveries:
            del self._deliveries[tag.method.delivery_tag]

    def _publisher(self):
        """This callback is used to check at regular interval if there's any
        message to be published to RabbitMQ.
        """
        if not self._connection.close or not self._connection.closing:
            try:
                for i in range(5):
                    pt = self.pq.get(False)
                    self._handle_publish(pt)
            except Empty:
                pass

        self._connection.add_timeout(.1, self._publisher)

    def _check_redeliveries(self):
        # In case we have a message to redeliver, let's wait a few seconds
        # before we actually redeliver them. This is to avoid unwanted
        # redeliveries.
        for key, value in self._deliveries.items():
            delta = datetime.now() - value['ts']
            task = value['task']
            if delta > timedelta(seconds=self.redelivery_timeout):
                self.logger.debug("[AMQP-REPLUBLISHED] #%s: %s" %
                                  (key, task.body))
                self.pq.put(task)
                del self._deliveries[key]
        self._connection.add_timeout(.1, self._check_redeliveries)

    def _handle_publish(self, publish_task):
        """This method actually publishes the item to the broker after
        sanitizing it from unwanted informations.
        """
        publish_args = publish_task.get()
        self._publish_channel.basic_publish(**publish_args)

        self._message_number += 1
        self.logger.debug("[AMQP-PUBLISHED] #%s: %s" %
                         (self._message_number, publish_task.body))
        if publish_task.redeliver:
            self._deliveries[self._message_number] = {}
            self._deliveries[self._message_number]["task"] = publish_task
            self._deliveries[self._message_number]["ts"] = datetime.now()

        if publish_args['properties'].correlation_id is not None:
            self._processing = False
            self.next_get()
