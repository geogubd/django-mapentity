import logging
from datetime import datetime

from django.conf import settings
from django.http import (HttpResponse, HttpResponseBadRequest,
                         HttpResponseServerError)
from django.utils.translation import ugettext_lazy as _

from django.utils.encoding import force_text
from django.views.generic.detail import DetailView
from django.views.generic.edit import CreateView, UpdateView, DeleteView
from django.views.generic.list import ListView
from django.core.cache import get_cache
from django.template.base import TemplateDoesNotExist
from django.template.defaultfilters import slugify
from django.contrib import messages
from django.contrib.admin.models import LogEntry, ADDITION, CHANGE, DELETION
from djgeojson.views import GeoJSONLayerView
from djappypod.odt import get_template
from djappypod.response import OdtTemplateResponse

from .. import API_SRID
from .. import app_settings
from .. import models as mapentity_models
from ..helpers import convertit_url, download_to_stream, user_has_perm
from ..decorators import save_history, view_permission_required, view_cache_latest
from ..serializers import GPXSerializer, CSVSerializer, DatatablesSerializer, ZipShapeSerializer
from ..filters import MapEntityFilterSet
from .base import history_delete
from .mixins import ModelViewMixin, JSONResponseMixin


logger = logging.getLogger(__name__)


def log_action(request, object, action_flag):
    LogEntry.objects.log_action(
        user_id=request.user.pk,
        content_type_id=object.get_content_type_id(),
        object_id=object.pk,
        object_repr=force_text(object),
        action_flag=action_flag
    )


class MapEntityLayer(ModelViewMixin, GeoJSONLayerView):
    """
    Take a class attribute `model` with a `latest_updated` method used for caching.
    """

    force2d = True
    srid = API_SRID

    def __init__(self, *args, **kwargs):
        super(MapEntityLayer, self).__init__(*args, **kwargs)
        if self.model is None:
            self.model = self.queryset.model
        # Backward compatibility with django-geojson 1.X
        # for JS ObjectsLayer and rando-trekking application
        # TODO: remove when migrated
        properties = dict([(k, k) for k in self.properties])
        if 'id' not in self.properties:
            properties['id'] = 'pk'
        self.properties = properties

    @classmethod
    def get_entity_kind(cls):
        return mapentity_models.ENTITY_LAYER

    @view_permission_required()
    @view_cache_latest()
    def dispatch(self, *args, **kwargs):
        return super(MapEntityLayer, self).dispatch(*args, **kwargs)

    def render_to_response(self, context, **response_kwargs):
        cache = get_cache(app_settings['GEOJSON_LAYERS_CACHE_BACKEND'])
        key = '%s_%s_layer_json' % (self.request.LANGUAGE_CODE,
                                    self.model._meta.module_name)

        result = cache.get(key)
        latest = self.model.latest_updated()

        if result and latest:
            cache_latest, content = result
            # Not empty and still valid
            if cache_latest and cache_latest >= latest:
                return self.response_class(content=content, **response_kwargs)

        response = super(MapEntityLayer, self).render_to_response(context, **response_kwargs)
        cache.set(key, (latest, response.content))
        return response


class MapEntityList(ModelViewMixin, ListView):
    """

    A generic view list web page.

    """
    template_name = 'mapentity/entity_list.html'
    model = None
    filterform = None
    columns = []

    def __init__(self, *args, **kwargs):
        super(MapEntityList, self).__init__(*args, **kwargs)

        if self.model is None:
            self.model = self.queryset.model

        if self.filterform is None:
            class filterklass(MapEntityFilterSet):
                class Meta:
                    model = self.model
            self.filterform = filterklass
        self._filterform = self.filterform(None, self.queryset)

    @classmethod
    def get_entity_kind(cls):
        return mapentity_models.ENTITY_LIST

    def get_queryset(self):
        queryset = super(MapEntityList, self).get_queryset()
        # Filter queryset from possible serialized form
        self._filterform = self.filterform(self.request.GET or None,
                                           queryset=queryset)
        return self._filterform.qs

    @view_permission_required(login_url='login')
    def dispatch(self, request, *args, **kwargs):
        # Save last list visited in session
        request.session['last_list'] = request.path
        return super(MapEntityList, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(MapEntityList, self).get_context_data(**kwargs)
        context['filterform'] = self._filterform
        context['columns'] = self.columns

        perm_create = self.model.get_permission_codename(mapentity_models.ENTITY_CREATE)
        can_add = user_has_perm(self.request.user, perm_create)
        context['can_add'] = can_add

        perm_export = self.model.get_permission_codename(mapentity_models.ENTITY_FORMAT_LIST)
        can_export = user_has_perm(self.request.user, perm_export)
        context['can_export'] = can_export

        return context


class MapEntityJsonList(JSONResponseMixin, MapEntityList):
    """
    Return objects list as a JSON that will populate the Jquery.dataTables.
    """

    @classmethod
    def get_entity_kind(cls):
        return mapentity_models.ENTITY_JSON_LIST

    @view_permission_required()
    def dispatch(self, *args, **kwargs):
        return super(ListView, self).dispatch(*args, **kwargs)

    def get_context_data(self, **kwargs):
        """
        Override the most important part of JSONListView... (paginator)
        """
        serializer = DatatablesSerializer()
        return serializer.serialize(self.get_queryset(), fields=self.columns, model=self.model)


class MapEntityFormat(MapEntityList):
    """Make it  extends your EntityList"""
    DEFAULT_FORMAT = 'csv'

    @classmethod
    def get_entity_kind(cls):
        return mapentity_models.ENTITY_FORMAT_LIST

    @view_permission_required()
    def dispatch(self, *args, **kwargs):
        return super(ListView, self).dispatch(*args, **kwargs)

    def get_context_data(self, **kwargs):
        return {}

    def render_to_response(self, context, **response_kwargs):
        """Delegate to the fmt view function found at dispatch time"""
        formats = {
            'csv': self.csv_view,
            'shp': self.shape_view,
            'gpx': self.gpx_view,
        }
        extensions = {
            'shp': 'zip'
        }
        fmt_str = self.request.GET.get('format', self.DEFAULT_FORMAT)
        formatter = formats.get(fmt_str)
        if not formatter:
            logger.warning("Unknown serialization format '%s'" % fmt_str)
            return HttpResponseBadRequest()

        filename = '%s-%s-list' % (datetime.now().strftime('%Y%m%d-%H%M'),
                                   str(slugify(unicode(self.model._meta.verbose_name))))
        filename += '.%s' % extensions.get(fmt_str, fmt_str)
        response = formatter(request=self.request, context=context, **response_kwargs)
        response['Content-Disposition'] = 'attachment; filename=%s' % filename
        return response

    def csv_view(self, request, context, **kwargs):
        serializer = CSVSerializer()
        response = HttpResponse(mimetype='text/csv')
        serializer.serialize(queryset=self.get_queryset(), stream=response,
                             model=self.model, fields=self.columns, ensure_ascii=True)
        return response

    def shape_view(self, request, context, **kwargs):
        serializer = ZipShapeSerializer()
        response = HttpResponse(mimetype='application/zip')
        serializer.serialize(queryset=self.get_queryset(), model=self.model,
                             stream=response, fields=self.columns)
        response['Content-length'] = str(len(response.content))
        return response

    def gpx_view(self, request, context, **kwargs):
        serializer = GPXSerializer()
        response = HttpResponse(mimetype='application/gpx+xml')
        serializer.serialize(self.get_queryset(), model=self.model, stream=response,
                             geom_field=app_settings['GEOM_FIELD_NAME'])
        return response


class MapEntityMapImage(ModelViewMixin, DetailView):
    """
    A static file view, that serves the up-to-date map image (detail screenshot)
    On error, returns 404 status.
    """
    @classmethod
    def get_entity_kind(cls):
        return mapentity_models.ENTITY_MAPIMAGE

    @view_permission_required()
    def dispatch(self, *args, **kwargs):
        return super(MapEntityMapImage, self).dispatch(*args, **kwargs)

    def render_to_response(self, context, **response_kwargs):
        try:
            obj = self.get_object()
            obj.prepare_map_image(self.request.build_absolute_uri('/'))
            response = HttpResponse(mimetype='image/png')
            # Open image file, and writes to response
            with open(obj.get_map_image_path(), 'rb') as f:
                response.write(f.read())
            return response
        except mapentity_models.MapImageError as e:
            logger.exception(e)
            return HttpResponseServerError(repr(e))


class MapEntityDocument(ModelViewMixin, DetailView):
    response_class = OdtTemplateResponse

    @classmethod
    def get_entity_kind(cls):
        return mapentity_models.ENTITY_DOCUMENT

    def __init__(self, *args, **kwargs):
        super(MapEntityDocument, self).__init__(*args, **kwargs)
        # Try to load template for each lang and object detail
        name_for = lambda app, modelname, lang: "%s/%s%s%s.odt" % (app, modelname, lang, self.template_name_suffix)
        langs = ['_%s' % lang for lang, langname in app_settings['LANGUAGES']]
        langs.append('')   # Will also try without lang

        def smart_get_template():
            for appname, modelname in [(self.model._meta.app_label, self.model._meta.object_name.lower()),
                                       ("mapentity", "entity")]:
                for lang in langs:
                    try:
                        template_name = name_for(appname, modelname, lang)
                        get_template(template_name)  # Will raise if not exist
                        return template_name
                    except TemplateDoesNotExist:
                        pass
            return None

        found = smart_get_template()
        if not found:
            raise TemplateDoesNotExist(name_for(self.model._meta.app_label, self.model._meta.object_name.lower(), ''))
        self.template_name = found

    @view_permission_required()
    def dispatch(self, *args, **kwargs):
        return super(MapEntityDocument, self).dispatch(*args, **kwargs)

    def get_context_data(self, **kwargs):
        rooturl = self.request.build_absolute_uri('/')

        # Screenshot of object map is required, since present in document
        self.get_object().prepare_map_image(rooturl)
        html = self.get_object().get_attributes_html(rooturl)

        context = super(MapEntityDocument, self).get_context_data(**kwargs)
        context['datetime'] = datetime.now()
        context['STATIC_URL'] = self.request.build_absolute_uri(settings.STATIC_URL)[:-1]
        context['MEDIA_URL'] = self.request.build_absolute_uri(settings.MEDIA_URL)[:-1]
        context['MEDIA_ROOT'] = settings.MEDIA_ROOT + '/'
        context['attributeshtml'] = html
        context['_'] = _
        return context


class DocumentConvert(DetailView):
    """
    A proxy view to conversion server.
    """
    format = 'pdf'

    def source_url(self):
        raise NotImplementedError

    def render_to_response(self, context):
        source = self.request.build_absolute_uri(self.source_url())
        url = convertit_url(source, to_type=self.format)
        response = HttpResponse()
        download_to_stream(url, response, silent=True)
        return response


"""

    CRUD

"""


class MapEntityCreate(ModelViewMixin, CreateView):

    template_name = 'mapentity/entity_form.html'

    @classmethod
    def get_entity_kind(cls):
        return mapentity_models.ENTITY_CREATE

    @classmethod
    def get_title(cls):
        name = cls.model._meta.verbose_name
        if hasattr(name, '_proxy____args'):
            name = name._proxy____args[0]  # untranslated
        # Whole "add" phrase translatable, but not catched  by makemessages
        return _(u"Add a new %s" % name.lower())

    @view_permission_required(login_url=mapentity_models.ENTITY_LIST)
    def dispatch(self, *args, **kwargs):
        return super(MapEntityCreate, self).dispatch(*args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super(MapEntityCreate, self).get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super(MapEntityCreate, self).form_valid(form)
        messages.success(self.request, _("Created"))
        log_action(self.request, self.object, ADDITION)
        return response

    def form_invalid(self, form):
        messages.error(self.request, _("Your form contains errors"))
        return super(MapEntityCreate, self).form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super(MapEntityCreate, self).get_context_data(**kwargs)
        return context


class MapEntityDetail(ModelViewMixin, DetailView):

    template_name = 'mapentity/entity_detail.html'

    @classmethod
    def get_entity_kind(cls):
        return mapentity_models.ENTITY_DETAIL

    def get_title(self):
        return unicode(self.get_object())

    @view_permission_required(login_url=mapentity_models.ENTITY_LIST)
    @save_history()
    def dispatch(self, *args, **kwargs):
        return super(MapEntityDetail, self).dispatch(*args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(MapEntityDetail, self).get_context_data(**kwargs)
        context['activetab'] = self.request.GET.get('tab')
        context['empty_map_message'] = _("No map available for this object.")

        perm_update = self.model.get_permission_codename(mapentity_models.ENTITY_UPDATE)
        can_edit = user_has_perm(self.request.user, perm_update)
        context['can_edit'] = can_edit
        context['can_read_attachment'] = user_has_perm(self.request.user, 'read_attachment')
        context['can_add_attachment'] = user_has_perm(self.request.user, 'add_attachment')
        context['can_delete_attachment'] = user_has_perm(self.request.user, 'delete_attachment')

        return context


class MapEntityUpdate(ModelViewMixin, UpdateView):

    template_name = 'mapentity/entity_form.html'

    @classmethod
    def get_entity_kind(cls):
        return mapentity_models.ENTITY_UPDATE

    def get_title(self):
        return _("Edit %s") % self.get_object()

    @view_permission_required(login_url=mapentity_models.ENTITY_DETAIL)
    def dispatch(self, *args, **kwargs):
        return super(MapEntityUpdate, self).dispatch(*args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super(MapEntityUpdate, self).get_form_kwargs()
        kwargs['user'] = self.request.user

        perm_delete = self.model.get_permission_codename(mapentity_models.ENTITY_DELETE)
        can_delete = user_has_perm(self.request.user, perm_delete)
        kwargs['can_delete'] = can_delete
        return kwargs

    def form_valid(self, form):
        response = super(MapEntityUpdate, self).form_valid(form)
        messages.success(self.request, _("Saved"))
        log_action(self.request, self.object, CHANGE)
        return response

    def form_invalid(self, form):
        messages.error(self.request, _("Your form contains errors"))
        return super(MapEntityUpdate, self).form_invalid(form)

    def get_success_url(self):
        return self.get_object().get_detail_url()


class MapEntityDelete(ModelViewMixin, DeleteView):

    template_name = 'mapentity/entity_confirm_delete.html'

    @classmethod
    def get_entity_kind(cls):
        return mapentity_models.ENTITY_DELETE

    @view_permission_required(login_url=mapentity_models.ENTITY_DETAIL)
    def dispatch(self, *args, **kwargs):
        return super(MapEntityDelete, self).dispatch(*args, **kwargs)

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        log_action(self.request, self.object, DELETION)
        # Remove entry from history
        history_delete(request, path=self.object.get_detail_url())
        return super(MapEntityDelete, self).delete(request, *args, **kwargs)

    def get_success_url(self):
        return self.model.get_list_url()
