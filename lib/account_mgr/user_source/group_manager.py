"""
group_manager.py

(c) 2011 SpiderOak, Inc.

Provides the group management decision making; given sets of users
from both LDAP and SpiderOak, determines the changes required to make
SpiderOak fit the LDAP groups.
"""

import logging
import psycopg2

from account_mgr import api_interface
from account_mgr.user_source import ldap_source
from account_mgr import account_runner

_USERS_TO_CREATE_QUERY = '''
SELECT
l.uniqueid, l.email, l.givenname, l.surname, l.group_id
FROM ldap_users l
LEFT OUTER JOIN users u ON l.uniqueid = u.uniqueid
WHERE u.uniqueid IS NULL;
'''

_USERS_TO_ENABLE_QUERY = '''
SELECT
l.uniqueid, u.avatar_id
FROM ldap_users l
LEFT OUTER JOIN users u ON l.uniqueid = u.uniqueid
WHERE u.enabled IS FALSE;
'''

_USERS_TO_DISABLE_QUERY = '''
SELECT
u.uniqueid, u.avatar_id
FROM users u
LEFT OUTER JOIN ldap_users l ON u.uniqueid = l.uniqueid
WHERE l.uniqueid IS NULL;
'''

_USERS_TO_PLANCHANGE_QUERY = '''
SELECT
l.uniqueid, u.avatar_id, l.group_id
FROM ldap_users l
LEFT OUTER JOIN users u ON l.uniqueid = u.uniqueid
WHERE l.group_id != u.group_id;
'''

_USERS_TO_EMAILCHANGE_QUERY = '''
SELECT
l.uniqueid, u.avatar_id, l.email
FROM ldap_users l
LEFT OUTER JOIN users u ON l.uniqueid = u.uniqueid
WHERE l.email != u.email;
'''


def _process_query(db_conn, query, extras=None):
    log = logging.getLogger('_process_query')

    if extras is None:
        extras = []

    cur = db_conn.cursor()
    cur.execute(query)
    results = list()
    for row in cur.fetchall():
        userinfo = {'uniqueid' : row[0]}
        for index, extra in enumerate(extras):
            userinfo[extra] = row[index+1]
            
        if 'avatar_id' in extras:
            log.debug('Query processing avatar %d' % (userinfo['avatar_id'],))
        else:
            log.debug('Query processing avatar %s' % (userinfo['email'],))

        results.append(userinfo)

    return results

def _calculate_changes_against_db(db_conn, users):
    """
    Calculates the changes necessary by comparing our groups from LDAP to the DB.
    """
    log = logging.getLogger('calculate_changes')
    api_actions = dict()

    cur = db_conn.cursor()
    cur.execute("CREATE TEMPORARY TABLE ldap_users (LIKE users) ON COMMIT DROP;")
    cur.execute("ALTER TABLE ldap_users DROP COLUMN avatar_id;")
    cur.execute("ALTER TABLE ldap_users DROP COLUMN enabled;")
    cur.executemany("INSERT INTO ldap_users (uniqueid, email, givenname, surname, group_id) VALUES (%(uniqueid)s, %(email)s, %(firstname)s, %(lastname)s, %(group_id)s);",
                    users)
    cur.close()

    # Users to create.
    log.debug('Creating users:')
    api_actions['create'] = _process_query(db_conn, _USERS_TO_CREATE_QUERY,
                                           ['email', 'firstname', 
                                            'lastname', 'group_id'])
    log.debug('Enabling users:')
    api_actions['enable'] = _process_query(db_conn, _USERS_TO_ENABLE_QUERY,
                                           ['avatar_id'])
    log.debug('Disabling users:')
    api_actions['disable'] = _process_query(db_conn, _USERS_TO_DISABLE_QUERY,
                                            ['avatar_id'])
    log.debug('Group change:')
    api_actions['group'] = _process_query(db_conn, _USERS_TO_PLANCHANGE_QUERY,
                                         ['avatar_id', 'group_id'])
    log.debug('Email change:')
    api_actions['email'] = _process_query(db_conn, _USERS_TO_EMAILCHANGE_QUERY,
                                          ['avatar_id', 'email'])

    return api_actions


def run_group_management(config, db_conn):
    """
    Resolves differences between the LDAP and our idea of the SpiderOak user DB.
      
    :param config: configuration dict.  Should be the standard OpenManage setup.
    :param user_source: UserSource object to pull users from.
    :param db_conn: DB connection object
    """
    log = logging.getLogger('run_group_management')

    # First step, collect the users from the LDAP groups.
    ldap_conn = ldap_source.OMLDAPConnection(config["dir_uri"], config["dir_base_dn"], config["dir_user"], config["dir_password"])

    ldap_users = ldap_source.collect_groups(ldap_conn, config)
    change_groups = _calculate_changes_against_db(db_conn, ldap_users)

    runner = account_runner.AccountRunner(config, db_conn)
    runner.runall(change_groups)
    db_conn.commit()


def run_db_repair(config, db_conn):
    """Repairs the current user DB and billing API versus LDAP."""
    # TODO: figure out what to do when email addresses *don't* match.

    # Collect the users from LDAP, and insert into a temporary table.
    ldap_conn = ldap_source.OMLDAPConnection(config["dir_uri"],
                                             config["dir_base_dn"],
                                             config["dir_user"],
                                             config["dir_password"])

    ldap_users = ldap_source.collect_groups(ldap_conn, config)
    cur = db_conn.cursor()
    cur.execute("CREATE TEMPORARY TABLE ldap_users (LIKE users) ON COMMIT DROP;")
    cur.execute("ALTER TABLE ldap_users DROP COLUMN avatar_id;")
    cur.execute("ALTER TABLE ldap_users DROP COLUMN enabled;")
    cur.executemany("INSERT INTO ldap_users (uniqueid, email, givenname, surname, group_id) VALUES (%(uniqueid)s, %(email)s, %(firstname)s, %(lastname)s, %(group_id)s);",
                    ldap_users)
    
    # Collect the users from the SpiderOak BillingAPI, and insert into
    # a temporary table.
    spider_users = api_interface.fetch_users()
    cur = db_conn.cursor()
    cur.execute("CREATE TEMPORARY TABLE spider_users (LIKE users) ON COMMIT DROP;")
    cur.execute("ALTER TABLE spider_users DROP COLUMN uniqueid;")
    cur.executemany("INSERT INTO spider_users "
                    "(avatar_id, email, givenname, surname, group_id, enabled) VALUES "
                    "(%(avatar_id)s, %(email)s, %(firstname)s, %(lastname)s, "
                    "%(group_id)s, %(enabled)s);",
                    spider_users)    

    # Clear out the current database.
    cur.execute("DELETE FROM users;")

    # Insert rows into users where email addresses match.
    cur.execute("INSERT INTO users "
                "SELECT l.uniqueid, s.email, s.avatar_id, s.givenname, "
                "s.surname, s.group_id, s.enabled "
                "FROM ldap_users l JOIN spider_users AS s ON l.email = s.email ")

    db_conn.commit()
