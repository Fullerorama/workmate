# -*- coding: utf-8 -*-
import copy

from django.contrib import messages
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.urlresolvers import NoReverseMatch
from django.utils.translation import ugettext_lazy as _

from workmate.menus.base import Menu
from workmate.menus.exceptions import NamespaceAlreadyRegistered
from workmate.utils.django_load import load


def _build_nodes_inner_for_one_menu(nodes, menu_class_name):
    '''
    This is an easier to test "inner loop" building the menu tree structure
    for one menu (one language, one site)
    '''
    done_nodes = {}  # Dict of node.id:Node
    final_nodes = []

    # This is to prevent infinite loops - we need to compare the number of
    # times we see a specific node to "something", and for the time being,
    # it's the total number of nodes
    list_total_length = len(nodes)

    while nodes:
        # For when the node has a parent_id but we haven't seen it yet.
        # We must not append it to the final list in this case!
        should_add_to_final_list = True

        node = nodes.pop(0)

        # Increment the "seen" counter for this specific node.
        node._counter = getattr(node, '_counter', 0) + 1

        # Implicit namespacing by menu.__name__
        if not node.namespace:
            node.namespace = menu_class_name
        if node.namespace not in done_nodes:
            # We need to create the namespace dict to avoid KeyErrors
            done_nodes[node.namespace] = {}

        # If we have seen the parent_id already...
        if node.parent_id in done_nodes[node.namespace]:
            # Implicit parent namespace by menu.__name__
            if not node.parent_namespace:
                node.parent_namespace = menu_class_name
            parent = done_nodes[node.namespace][node.parent_id]
            parent.children.append(node)
            node.parent = parent
        # If it has a parent_id but we haven't seen it yet...
        elif node.parent_id:
            # We check for infinite loops here, by comparing the number of
            # times we "saw" this node to the number of nodes in the list
            if node._counter < list_total_length:
                nodes.append(node)
            # Never add this node to the final list until it has a real
            # parent (node.parent)
            should_add_to_final_list = False

        if should_add_to_final_list:
            final_nodes.append(node)
            # add it to the "seen" list
            done_nodes[node.namespace][node.id] = node
    return final_nodes


class MenuPool(object):
    def __init__(self):
        self.menus = {}
        self.modifiers = []
        self.discovered = False
        self._expanded = False

    def discover_menus(self):
        if self.discovered:
            return
        load('workmate_menus')
        from workmate.menus.modifiers import register
        register()
        self.discovered = True
        self._expanded = False

    def _expand_menus(self):
        """
        Expands the menu_pool by converting any found CMSAttachMenu entries to
        one entry for each instance they are attached to. and instantiates menus
        from the existing menu classes.
        """

        # Ideally, this would have been done in discover_menus(), but the pages
        # aren't loaded when that executes. This private method is used to
        # perform the expansion and instantiate the menus classes into menu-
        # instances just before any menus are built.

        if self._expanded:
            return
        expanded_menus = {}
        for menu_class_name, menu_cls in self.menus.items():
            # In order to be eligible for "expansion", the menu_cls must, in
            # fact, be an instantiable class. We are lenient about this here,
            # though, because the CMS has previously allowed attaching
            # CMSAttachMenu's as objects rather than classes.
            if isinstance(menu_cls, Menu):
                # A Menu **instance** was registered, this is non-standard, but
                # acceptable. However, it cannot be "expanded", so, just add it
                # as-is to the list of expanded_menus.
                menu_cls = menu_cls.__class__
            if hasattr(menu_cls, "get_instances"):
                # It quacks like a CMSAttachMenu, expand away!
                # If a menu exists but has no instances,
                # it's included in the available menus as is
                instances = menu_cls.get_instances()
                if not instances:
                    expanded_menus[menu_class_name] = menu_cls()
                else:
                    for instance in instances:
                        namespace = "{0}:{1}".format(
                            menu_class_name, instance.pk)
                        menu_inst = menu_cls()
                        menu_inst.instance = instance
                        expanded_menus[namespace] = menu_inst
            elif hasattr(menu_cls, "get_nodes"):
                # This is another type of Menu, cannot be expanded, but must be
                # instantiated, none-the-less.
                expanded_menus[menu_class_name] = menu_cls()
            else:
                raise ValidationError(
                    "Something was registered as a menu, but isn't.")

        self._expanded = True
        self.menus = expanded_menus

    def clear(self, site_id=None, language=None, all=False):
        '''
        This invalidates the cache for a given menu (site_id and language)
        '''
        pass

    def register_menu(self, menu_cls):
        from workmate.menus.base import Menu
        assert issubclass(menu_cls, Menu)
        # If we should register a menu after we've already expanded the existing
        # ones, we need to mark it as such.
        self._expanded = False
        if menu_cls.__name__ in self.menus.keys():
            raise NamespaceAlreadyRegistered(
                "[{0}] a menu with this name is already registered".format(
                    menu_cls.__name__))
        # Note: menu_cls should still be the menu CLASS at this point. It will
        # be instantiated in self._expand_menus().
        self.menus[menu_cls.__name__] = menu_cls

    def register_modifier(self, modifier_class):
        from workmate.menus.base import Modifier
        assert issubclass(modifier_class, Modifier)
        if modifier_class not in self.modifiers:
            self.modifiers.append(modifier_class)

    def _build_nodes(self, request, site_id):
        self._expand_menus()

        final_nodes = []
        for menu_class_name in self.menus:
            menu = self.menus[menu_class_name]
            try:
                if isinstance(menu, type):
                    menu = menu()
                nodes = menu.get_nodes(request)
            except NoReverseMatch:
                # Apps might raise NoReverseMatch if an apphook does not yet
                # exist, skip them instead of crashing
                nodes = []
                toolbar = getattr(request, 'toolbar', None)
                if toolbar and toolbar.is_staff:
                    messages.error(request,
                        _('Menu %s cannot be loaded. Please, make sure all '
                          'its urls exist and can be resolved.') %
                        menu_class_name)
            # nodes is a list of navigation nodes (page tree in cms + others)
            final_nodes += _build_nodes_inner_for_one_menu(
                nodes, menu_class_name)

        return final_nodes

    def apply_modifiers(self, nodes, request, namespace=None, root_id=None,
            post_cut=False, breadcrumb=False):
        if not post_cut:
            nodes = self._mark_selected(request, nodes)
        for cls in self.modifiers:
            inst = cls()
            nodes = inst.modify(
                request, nodes, namespace, root_id, post_cut, breadcrumb)
        return nodes

    def get_nodes(self, request, namespace=None, root_id=None, site_id=None,
            breadcrumb=False):
        self.discover_menus()
        if not site_id:
            site_id = Site.objects.get_current().pk
        nodes = self._build_nodes(request, site_id)
        nodes = copy.deepcopy(nodes)
        nodes = self.apply_modifiers(nodes, request, namespace, root_id,
                                     post_cut=False, breadcrumb=breadcrumb)
        return nodes

    def _mark_selected(self, request, nodes):
        # There /may/ be two nodes that get marked with selected. A published
        # and a draft version of the node. We'll mark both, later, the unused
        # one will be removed anyway.
        sel = []
        for node in nodes:
            node.sibling = False
            node.ancestor = False
            node.descendant = False
            node_abs_url = node.get_absolute_url()
            if node_abs_url == request.path[:len(node_abs_url)]:
                if sel:
                    if len(node_abs_url) > len(sel[0].get_absolute_url()):
                        sel = [node]
                    elif len(node_abs_url) == len(sel[0].get_absolute_url()):
                        sel.append(node)
                else:
                    sel = [node]
        for node in nodes:
            node.selected = (node in sel)
        return nodes

    def get_menus_by_attribute(self, name, value):
        """
        Returns the list of menus that match the name/value criteria provided.
        """
        # Note that we are limiting the output to only single instances of any
        # specific menu class. This is to address issue (#4041) which has
        # cropped-up in 3.0.13/3.0.0.
        self.discover_menus()
        self._expand_menus()
        return sorted(list(set([(menu.__class__.__name__, menu.name)
                                for menu_class_name, menu in self.menus.items()
                                if getattr(menu, name, None) == value])))

    def get_nodes_by_attribute(self, nodes, name, value):
        return [node for node in nodes if node.attr.get(name, None) == value]

menu_pool = MenuPool()