﻿# Copyright (c) 2015  aggftw@gmail.com
# Distributed under the terms of the Modified BSD License.

import textwrap
from time import sleep

from .log import Log
from .constants import Constants
from .livyclienttimeouterror import LivyClientTimeoutError


class LivySession(object):
    """Session that is livy specific."""
    logger = Log()

    # TODO(aggftw): do a pass to remove all strings and consolidate into variables
    _idle_session_state = "idle"
    _possible_session_states = ['not_started', _idle_session_state, 'starting', 'busy', 'error', 'dead']

    def __init__(self, http_client, language, session_id, sql_created,
                 state_sleep_seconds=2, statement_sleep_seconds=2, create_sql_context_timeout_seconds=60):
        # TODO(aggftw): make threadsafe
        assert state_sleep_seconds > 0
        assert statement_sleep_seconds >= 0
        assert create_sql_context_timeout_seconds >= 0
        if session_id == "-1" and sql_created is True:
            raise ValueError("Cannot indicate sql state without session id.")

        language = language.lower()
        if language not in Constants.lang_supported:
            raise ValueError("Session of language '{}' not supported. Session must be of languages {}."
                             .format(language, ", ".join(Constants.lang_supported)))

        if session_id == "-1":
            self._state = "not_started"
            self._started_sql_context = False
        else:
            self._state = "busy"
            self._started_sql_context = sql_created
        self._id = session_id
        self._http_client = http_client
        self._language = language
        self._state_sleep_seconds = state_sleep_seconds
        self._statement_sleep_seconds = statement_sleep_seconds
        self._create_sql_context_timeout_seconds = create_sql_context_timeout_seconds

    def serialize(self):
        serialized = self._http_client.serialize()
        serialized["id"] = self._id
        serialized["language"] = self._language
        serialized["sqlcontext"] = self._started_sql_context
        serialized["version"] = "0.0.0"
        return serialized

    def start(self):
        """Start the session against actual livy server."""
        # TODO(aggftw): do a pass to make all contracts variables; i.e. not peppered in code
        self.logger.debug("Starting '{}' session.".format(self._language))

        r = self._http_client.post("/sessions", [201], {"kind": self._get_livy_kind()})
        self._id = str(r.json()["id"])
        self._state = str(r.json()["state"])

        self.logger.debug("Session '{}' started.".format(self._language))

    def create_sql_context(self):
        """Create a sqlContext object on the session. Object will be accessible via variable 'sqlContext'."""
        if self._started_sql_context:
            return

        self.logger.debug("Starting '{}' sql session.".format(self._language))

        self.wait_for_state(self._idle_session_state, self._create_sql_context_timeout_seconds)

        self.execute(self._get_sql_context_creation_command())

        self._started_sql_context = True

        self.logger.debug("Started '{}' sql session.".format(self._language))

    @property
    def id(self):
        return self._id

    @property
    def started_sql_context(self):
        return self._started_sql_context

    @property
    def language(self):
        return self._language

    @property
    def state(self):
        state = self._get_session_state()

        if state in self._possible_session_states:
            self._state = state
        else:
            raise ValueError("State '{}' not supported by session.".format(state))

        return self._state

    @property
    def http_client(self):
        return self._http_client
    
    def execute(self, commands):
        """Executes commands in session."""
        code = textwrap.dedent(commands)

        self.logger.debug("Executing code:\n{}\nFrom commands: {}".format(code, commands))

        data = {"code": code}
        r = self._http_client.post(self._statements_url(), [201], data)
        statement_id = r.json()['id']
        
        return self._get_statement_output(statement_id)

    def delete(self):
        """Deletes the session and releases any resources."""
        self.logger.debug("Deleting session '{}'".format(self._id))

        if self._state != "not_started" and self._state != "dead":
            self._http_client.delete("/sessions/{}".format(self._id), [200, 404])
            self._state = 'dead'
        else:
            raise ValueError("Cannot delete session {} that is in state '{}'."
                             .format(self._id, self._state))

    def wait_for_state(self, state, seconds_to_wait):
        """Wait for session to be in a certain state. Sleep meanwhile. Calls done every state_sleep_seconds as
        indicated by the constructor."""
        current_state = self.state
        if current_state == state:
            return
        elif seconds_to_wait > 0:
            self.logger.debug("Session {} in state {}. Sleeping {} seconds."
                              .format(self._id, current_state, seconds_to_wait))
            sleep(self._state_sleep_seconds)
            return self.wait_for_state(state, seconds_to_wait - self._state_sleep_seconds)
        else:
            raise LivyClientTimeoutError("Session {} did not reach {} state in time. Current state is {}."
                                         .format(self._id, state, current_state))

    def _statements_url(self):
        return "/sessions/{}/statements".format(self._id)

    def _get_session_state(self):
        """Get current session state. Network call."""
        r = self._http_client.get("/sessions", [200])
        sessions = r.json()["sessions"]
        filtered_sessions = [s for s in sessions if s["id"] == int(self._id)]
                    
        if len(filtered_sessions) != 1:
            raise AssertionError("Expected one session of id {} but got {} sessions."
                                 .format(self._id, len(filtered_sessions)))
            
        session = filtered_sessions[0]
        return session['state']
    
    def _get_statement_output(self, statement_id):
        statement_running = True
        output = ""
        while statement_running:
            r = self._http_client.get(self._statements_url(), [200])
            statement = [i for i in r.json()["statements"] if i["id"] == statement_id][0]
            state = statement["state"]

            self.logger.debug("State of statement {} is {}.".format(statement_id, state))

            if state == "running":
                sleep(self._statement_sleep_seconds)
            else:
                statement_running = False
                
                statement_output = statement["output"]
                if statement_output["status"] == "ok":
                    output = statement_output["data"]["text/plain"]
                elif statement_output["status"] == "error":
                    output = statement_output['evalue']

        self.logger.debug("Output of statement {} is {}.".format(statement_id, output))
        return output

    def _get_livy_kind(self):
        if self.language == Constants.lang_scala:
            return Constants.session_kind_spark
        elif self.language == Constants.lang_python:
            return Constants.session_kind_pyspark
        else:
            raise ValueError("Cannot get session kind for {}.".format(self.language))

    def _get_sql_context_creation_command(self):
        if self.language == Constants.lang_scala:
            sql_context_command = "val sqlContext = new org.apache.spark.sql.SQLContext(sc)\n" \
                                  "import sqlContext.implicits._"
        elif self.language == Constants.lang_python:
            sql_context_command = "from pyspark.sql import SQLContext\nfrom pyspark.sql.types import *\n" \
                                  "sqlContext = SQLContext(sc)"
        else:
            raise ValueError("Do not know how to create sqlContext in session of language {}.".format(self.language))

        return sql_context_command
