# -*- coding: utf-8 -*-
## This file is part of Invenio.
## Copyright (C) 2011, 2012 CERN.
##
## Invenio is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 2 of the
## License, or (at your option) any later version.
##
## Invenio is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Invenio; if not, write to the Free Software Foundation, Inc.,
## 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""
Invenio -> Flask adapter
"""

import os
import hashlib
from os.path import join, exists, getmtime, splitext
from pprint import pformat
from datetime import datetime
from logging.handlers import RotatingFileHandler
from logging import Formatter
from flask import Blueprint, Flask, logging, session, request, g, \
                url_for, current_app, Response, render_template
from jinja2 import FileSystemLoader, MemcachedBytecodeCache
from werkzeug.routing import BuildError, NotFound, RequestRedirect

from invenio.sqlalchemyutils import db
from invenio import config
from invenio.errorlib import register_exception
from invenio.config import CFG_PYLIBDIR, \
    CFG_WEBSESSION_EXPIRY_LIMIT_REMEMBER, \
    CFG_BIBDOCFILE_USE_XSENDFILE, \
    CFG_LOGDIR, CFG_SITE_LANG, CFG_WEBDIR, \
    CFG_ETCDIR, CFG_DEVEL_SITE, \
    CFG_FLASK_CACHE_TYPE, \
    CFG_SITE_URL, CFG_SITE_SECURE_URL
from invenio.websession_config import CFG_WEBSESSION_COOKIE_NAME, CFG_WEBSESSION_ONE_DAY

CFG_HAS_HTTPS_SUPPORT = CFG_SITE_SECURE_URL.startswith("https://")
CFG_FULL_HTTPS = CFG_SITE_URL.lower().startswith("https://")

def create_invenio_flask_app():
    """
    This prepare wsgi Invenio application based on Flask.
    """

    ## The Flask application instance
    _app = Flask(__name__,
        static_url_path='/', ## We assume anything under '/' which is static to be handled directly by Apache
        static_folder=CFG_WEBDIR)

    ## Let's initialize database.
    db.init_invenio()
    db.init_cfg(_app)
    # Register Flask application in flask-sqlalchemy extension
    db.init_app(_app)

    from invenio.pluginutils import PluginContainer, create_enhanced_plugin_builder
    from invenio.session_flask import InvenioSessionInterface
#from flaskext.login import LoginManager
    from invenio.webuser_flask import InvenioLoginManager, current_user
    from invenio.messages import wash_language, gettext_set_language, language_list_long, is_language_rtl
    from invenio.dateutils import convert_datetext_to_dategui, \
                                  convert_datestruct_to_dategui
    from invenio.urlutils import create_url
    from invenio.cache import cache
#from invenio.webuser import collect_user_info
    from invenio.jinja2utils import CollectionExtension
    from invenio.webmessage_mailutils import email_quoted_txt2html
    from flask.ext.assets import Environment, Bundle
    from invenio.webinterface_handler_flask_utils import unicodifier
    from flaskext.gravatar import Gravatar
    from werkzeug.wrappers import BaseResponse
    from werkzeug.exceptions import HTTPException
    from invenio.flask_sslify import SSLify
    from invenio.webinterface_handler_wsgi import \
                application as legacy_application

    if CFG_HAS_HTTPS_SUPPORT:
        # Makes request always run over HTTPS.
        _sslify = SSLify(_app)

        if not CFG_FULL_HTTPS:
            @_sslify.criteria_handler
            def criteria():
                """Extends criteria when to stay on HTTP site."""
                _force_https = False
                if request.blueprint in current_app.blueprints:
                    _force_https = current_app.blueprints[request.blueprint]._force_https

                view_func = current_app.view_functions.get(request.endpoint)
                if view_func is not None and hasattr(view_func, '_force_https'):
                    _force_https = view_func._force_https

                return not (_force_https or session.need_https())


    class LegacyAppMiddleware(object):
        def __init__(self, app):
            self.app = app

        def __call__(self, environ, start_response):
            with self.app.request_context(environ):
                g.start_response = start_response
                try:
                    response = self.app.full_dispatch_request()
                except Exception, e:
                    response = self.app.make_response(self.app.handle_exception(e))
                    #raise
                    #register_exception(alert_admin=True)
                return response(environ, start_response)

    _app.wsgi_app = LegacyAppMiddleware(_app)

    @_app.errorhandler(404)
    def page_not_found(error):
        try:
            response = legacy_application(request.environ, g.start_response)
            if not isinstance(response, BaseResponse):
                response = str(response) #current_app.make_response(response)
            return response
        except HTTPException, e:
            return render_template("404.html"), 404

    if CFG_FLASK_CACHE_TYPE not in [None, 'null']:
        _app.jinja_options = dict(_app.jinja_options,
            auto_reload=False,
            cache_size=-1,
            bytecode_cache=MemcachedBytecodeCache(
                                cache, prefix="jinja::",
                                timeout=3600))


    ## Let's customize the template loader to first look into
    ## /opt/invenio/etc-local/templates and then into /opt/invenio/etc/templates
    _app.jinja_loader = FileSystemLoader([join(CFG_ETCDIR + '-local', 'templates'), join(CFG_ETCDIR, 'templates')])

    ## Let's attach our session handling (which is bridging with the native Invenio session handling
    _app.session_interface = InvenioSessionInterface()

    ## Let's load the whole invenio.config into Flask :-) ...
    _app.config.from_object(config)

    ## ... and map certain common parameters
    _app.config["SECRET_KEY"] = 'Inv3n10'
    _app.config['SESSION_COOKIE_NAME'] = CFG_WEBSESSION_COOKIE_NAME
    _app.config['PERMANENT_SESSION_LIFETIME'] = CFG_WEBSESSION_EXPIRY_LIMIT_REMEMBER * CFG_WEBSESSION_ONE_DAY
    _app.config['USE_X_SENDFILE'] = CFG_BIBDOCFILE_USE_XSENDFILE
    _app.config['DEBUG'] = False #CFG_DEVEL_SITE
    _app.debug = False #True #CFG_DEVEL_SITE
    _app.config['CFG_LANGUAGE_LIST_LONG'] = [(lang, longname.decode('utf-8')) for (lang, longname) in language_list_long()]

    ## Invenio is all using str objects. Let's change them to unicode
    _app.config.update(unicodifier(dict(_app.config)))

    ## Cache
    _app.config['CACHE_TYPE'] = CFG_FLASK_CACHE_TYPE
    cache.init_app(_app)
    if CFG_FLASK_CACHE_TYPE == 'redis':
        def with_try_except_block(f):
            def decorator(*args, **kwargs):
                try:
                    return f(*args, **kwargs)
                except Exception, e:
                    register_exception(alert_admin=True)
                    pass
            return decorator

        ## When the redis is down, we would like to keep the site running.
        cache.cache._client.execute_command = with_try_except_block(cache.cache._client.execute_command)

    _flask_log_handler = RotatingFileHandler(os.path.join(CFG_LOGDIR, 'flask.log'))
    _flask_log_handler.setFormatter(Formatter(
        '%(asctime)s %(levelname)s: %(message)s '
        '[in %(pathname)s:%(lineno)d]'
    ))
    _app.logger.addHandler(_flask_log_handler)

    # Let's create login manager.
    _login_manager = InvenioLoginManager()
    _login_manager.login_view = 'youraccount.login'
    _login_manager.setup_app(_app)

    # Let's create main menu.
    class Menu(object):
        def __init__(self, id='', title='', url='', order=None, children=None,
                     display=lambda:True):
            self.id = id
            self.title = title
            self.url = url
            self.children = children or {}
            self.order = order or 100
            self.display = display

    # Let's create assets environment.
    _assets = Environment(_app)
    _assets.debug = True #config.CFG_DEVEL_SITE == 1
    _assets.directory = config.CFG_WEBDIR

    _app.jinja_env.extend(new_bundle=lambda tag, collection: \
        Bundle(output="%s/invenio-%s.%s" % \
               (tag, hash('|'.join(collection)), tag),
               *collection))
    _app.jinja_env.add_extension(CollectionExtension)
    _app.jinja_env.add_extension('jinja2.ext.do')

    # Let's create Gravatar bridge.
    _gravatar = Gravatar(_app,
                    size=100,
                    rating='g',
                    default='retro',
                    force_default=False,
                    force_lower=False)

    @_login_manager.user_loader
    def _load_user(uid):
        """
        Function should not raise an exception if uid is not valid
        or User was not found in database.
        """
        from invenio.webuser_flask import UserInfo
        return UserInfo(uid)

    @_app.before_request
    def reset_template_context_processor():
        g._template_context_processor = []

    @_app.context_processor
    def _inject_template_context():
        context = {}
        for func in g._template_context_processor:
            context.update(func())
        return context

    @_app.before_request
    def _guess_language():
        """
        Before every request being handled, let's compute the language needed to
        return the answer to the client.

        This information will then be available in the session['ln'] and in g.ln.

        Additionally under g._ an already configured internationalization function
        will be available (configured to return unicode objects).
        """
        required_ln = None
        try:
            values = request.values
        except:
            values = {}
        if "ln" in values:
            ## If ln is specified explictly as a GET or POST argument
            ## let's take it!
            passed_ln = str(values["ln"])
            required_ln = wash_language(passed_ln)
            if passed_ln != required_ln:
                ## But only if it was a valid language
                required_ln = None
        if required_ln:
            ## Ok it was. We store it in the session.
            session["ln"] = required_ln
        if not "ln" in session:
            ## If there is no language saved into the session...
            if "user_info" in session and session["user_info"].get("language"):
                ## ... and the user is logged in, we try to take it from its
                ## settings.
                session["ln"] = session["user_info"]["language"]
            else:
                ## Otherwise we try to guess it from its request headers
                for value, quality in request.accept_languages:
                    value = str(value)
                    ln = wash_language(value)
                    if ln == value or ln[:2] == value[:2]:
                        session["ln"] = ln
                        break
                else:
                    ## Too bad! We stick to the default :-)
                    session["ln"] = CFG_SITE_LANG
        ## Well, let's make it global now
        g.ln = session["ln"]
        g._ = gettext_set_language(g.ln, use_unicode=True)

    @_app.template_filter('quoted_txt2html')
    def _quoted_txt2html(*args, **kwargs):
        return email_quoted_txt2html(*args, **kwargs)

    @_app.template_filter('invenio_format_date')
    def _format_date(date):
        """
        This is a special Jinja2 filter that will call convert_datetext_to_dategui
        to print a human friendly date.
        """
        if isinstance(date, datetime):
            return convert_datestruct_to_dategui(date.timetuple(), g.ln).decode('utf-8')
        return convert_datetext_to_dategui(date, g.ln).decode('utf-8')

    @_app.template_filter('invenio_url_args')
    def _url_args(d, append=u'?', filter=[]):
        from jinja2.utils import escape
        rv = append + u'&'.join(
            u'%s=%s' % (escape(key), escape(value))
            for key, value in d.iteritems(True)
            if value is not None and key not in filter
            # and not isinstance(value, Undefined)
        )
        return rv

    @_app.context_processor
    def _inject_utils():
        """
        This will add some more variables and functions to the Jinja2 to execution
        context. In particular it will add:

        - `url_for`: an Invenio specific wrapper of Flask url_for, that will let you
                     obtain URLs for non Flask-native handlers (i.e. not yet ported
                     Invenio URLs)
        - `breadcrumbs`: this will be a list of three-elements tuples, containing
                     the hierarchy of Label -> URLs of navtrails/breadcrumbs.
        - `_`: this can be used to automatically translate a given string.
        - `is_language_rtl`: is True if the chosen language should be read right to left
        """
        def invenio_url_for(endpoint, **values):
            try:
                return url_for(endpoint, **values)
            except BuildError:
                if endpoint.startswith('.'):
                    endpoint = request.blueprint + endpoint
                return create_url('/' + '/'.join(endpoint.split('.')), values, False).decode('utf-8')

        if request.endpoint in current_app.config['breadcrumbs_map']:
            breadcrumbs = current_app.config['breadcrumbs_map'][request.endpoint]
        elif request.endpoint:
            breadcrumbs = [(_('Home'), '')] + current_app.config['breadcrumbs_map'].get(request.endpoint.split('.')[0], [])
        else:
            breadcrumbs = [(_('Home'), '')]

        menubuilder = filter(lambda x: x.display(), current_app.config['menubuilder_map']['main'].\
                        children.itervalues())

        def get_css_bundle():
            collection = get_css_collection()
            #TODO add settings for CSS filters
            return Bundle(output='css/invenio-%s.css' % \
                          hash('|'.join(collection)), *collection)

        def get_js_bundle():
            collection = get_js_collection()
            #TODO add settings for JS minifiers
            return Bundle(output='js/invenio-%s.js' % \
                          hash('|'.join(collection)), *collection)

        user = current_user._get_current_object()
        return dict(_=g._,
                    current_user=user,
                    get_css_bundle=_app.jinja_env.get_css_bundle,
                    get_js_bundle=_app.jinja_env.get_js_bundle,
                    is_language_rtl=is_language_rtl(g.ln),
                    url_for=invenio_url_for,
                    breadcrumbs=breadcrumbs,
                    menu=menubuilder)

    def _invenio_blueprint_plugin_builder(plugin_name, plugin_code):
        """
        Handy function to bridge pluginutils with (Invenio) blueprints.
        """
        from invenio.webinterface_handler_flask_utils import InvenioBlueprint
        if 'blueprint' in dir(plugin_code):
            candidate = getattr(plugin_code, 'blueprint')
            if isinstance(candidate, InvenioBlueprint):
                return candidate
        raise ValueError('%s is not a valid blueprint plugin' % plugin_name)


## Let's load all the blueprints that are composing this Invenio instance
    _BLUEPRINTS = PluginContainer(
        os.path.join(CFG_PYLIBDIR, 'invenio', '*_blueprint.py'),
        plugin_builder=_invenio_blueprint_plugin_builder)

## Let's report about broken plugins
    open(join(CFG_LOGDIR, 'broken-blueprints.log'), 'w').write(
        pformat(_BLUEPRINTS.get_broken_plugins()))

    _app.config['breadcrumbs_map'] = {}
    _app.config['menubuilder_map'] = {}

## Let's attach all the blueprints
    from invenio.webinterface_handler_flask_utils import _
    for plugin in _BLUEPRINTS.values():
        _app.register_blueprint(plugin)
        if plugin.config:
            ## Let's include the configuration parameters of the config file.
            ## E.g. if the blueprint specify the config string 'invenio.webmessage_config'
            ## any uppercase variable defined in the module invenio.webmessage_config
            ## is loaded into the system.
            _app.config.from_object(plugin.config)
        if plugin.breadcrumbs:
            _app.config['breadcrumbs_map'][plugin.name] = plugin.breadcrumbs
        _app.config['breadcrumbs_map'].update(plugin.breadcrumbs_map)

        ## Let's build global menu. Each blueprint can plug its own menu items.
        if plugin.menubuilder:
            _app.config['menubuilder_map'].update((m[0],Menu(*m)) for m in plugin.menubuilder)
        _app.config['menubuilder_map'].update(plugin.menubuilder_map)

    _app.config['menubuilder_map'].update({
            'main.admin': Menu('main.admin', _('Administration'),
                                'help.admin', 9998, [],
                                lambda: current_user.is_admin),
            'main.help': Menu('main.help', _('Help'), 'help', 9999)})

    menu = {'main': Menu('main', '', '')}
    for key,item in _app.config['menubuilder_map'].iteritems():
        start = menu

        if '.' not in key:
            menu[key] = item
            continue

        keys = key.split('.')
        for k in keys[:-1]:
            try:
                start = start[k].children
            except:
                start[k] = Menu()
                start = start[k].children

        if keys[-1] in start:
            item.children.update(start[keys[-1]].children)
        start[keys[-1]] = item

    _app.config['menubuilder_map'] = menu

    try:
        ## When deploying Invenio, one can prepare a module called
        ## webinterface_handler_local.py to be deployed under
        ## CFG_PYLIBDIR/invenio directory, and containing a function called
        ## customize_app which should accept a Flask application.
        ## This function has a chance to modify the application as needed
        ## including changing the URL routing map.
        # pylint: disable=E0611
        from invenio.webinterface_handler_local import customize_app
        # pylint: enable=E0611
        customize_app(_app)
    except ImportError:
        ## No customization needed.
        pass

    return _app
