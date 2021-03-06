from bs4 import BeautifulSoup
from accessor_functions import *
from generate_dot import *
from DiffObject import DiffObject
from rate_laws import *
from miriam import align_models
from tabulate import tabulate
import sys
import re


class SBMLDiff:

    def __init__(self, model_strings, model_names, generate_dot, align=False, cartoon=False, show_params=True, hide_rules=False, use_sympy=""):
        """

        Parameters
        ----------
        model_strings : a list, in which each element is an SBML model as a string
        model_names : names of each model (used as headings for the columns in table)
        generate_dot : instance of the GenerateDot class
        align : Boolean indicating whether to try to match using MIRIAM annotations as well as reaction/species id
        cartoon : Boolean indicating whether to draw transcription as a SBOLv promoter/CSD glyph, and hide degredation

        Returns
        -------
        models : list of models (each a bs4.BeautifulSoup object produced by parsing an SBML model)

        """

        self.model_strings = model_strings
        self.model_names = model_names
        self.generate_dot = generate_dot
        self.align = align
        self.cartoon = cartoon
        self.show_params = show_params
        self.hide_rules = hide_rules
        self.use_sympy = use_sympy

        self.diff_object = DiffObject()

        self.models = map(lambda x: BeautifulSoup(x, 'xml'), self.model_strings)

        # Avoid need to search for reactions by id
        self.reactions = []
        for model in self.models:
            mr = {}
            reaction_list = model.select_one("listOfReactions")
            if reaction_list:
                for reaction in reaction_list.select("reaction"):
                    reaction_id = reaction.attrs["id"]
                    mr[reaction_id] = reaction
            self.reactions.append(mr)

        # avoid need to keep finding reactant compartments
        self.species_compartment = []
        self.initial_value = []
        for model in self.models:
            tmp = {}
            tmp_concentrations = {}

            species_list = model.select_one("listOfSpecies")
            if species_list:
                for species in species_list.select("species"):
                    species_id = species.attrs["id"]
                    compartment = species.attrs["compartment"]
                    tmp[species_id] = compartment

                    if "initialConcentration" in species.attrs:
                        tmp_concentrations[species_id] = species.attrs["initialConcentration"]

            self.species_compartment.append(tmp)
            self.initial_value.append(tmp_concentrations)

        # get initial parameter values
        self.initial_parameters = []
        for model_num, model in enumerate(self.models):
            for param in model.select("parameter"):
                if "id" not in param.attrs.keys():
                    continue
                param_id = param.attrs["id"]
                if "value" in param.attrs:
                    self.initial_value[model_num][param_id] = param.attrs["value"]

        # avoid need to search for reaction name
        self.reaction_name = []
        for model in self.models:
            tmp = {}

            reaction_list = model.select_one("listOfReactions")
            if reaction_list:
                for r in reaction_list.select("reaction"):
                    reaction_id = r.attrs["id"]
                    if "name" in r.attrs.keys() and r.attrs["name"]:
                        tmp[reaction_id] = r.attrs["name"]
                    else:
                        tmp[reaction_id] = reaction_id

            self.reaction_name.append(tmp)

        if self.cartoon:
            self.elided_list = []
            self.elided_reactions = []
            self.downstream_species = []
            self.find_downstream_species()

        self.modified_params = {}

    def check_model_supported(self):
        """
        Print an error message and quit if the file cannot be processed (because it contains user-defined functions, or is
        missing a list of species), rather than dumping a stack trace.
        """
        for model in self.models:

            if model.select_one('listOfReactions') and not model.select_one('listOfSpecies'):
                raise RuntimeError("Every model that includes a listOfReactions must include a listOfSpecies.")

            if not model.select_one('sbml') or 'xmlns' not in model.select_one('sbml').attrs.keys():
                raise RuntimeError("Every file must be an sbml model")

            if "level1" in model.select_one('sbml').attrs['xmlns']:
                raise RuntimeError("Every model must be in SBML level 2 or higher, since sbml-diff relies on id attributes")

    def print_rate_law_table(self, output_format="simple"):
        """
        Print a table of kineticLaws, in which rows correspond to reactions and columns to models.

        Parameters
        ----------
        output_format : a table format supported by tabulate (e.g. simple, html)
        """

        # get list of all reactions in all models
        reactions = []
        for model in self.models:
            reactions.extend(get_reactions(model))
        reactions = list(set(reactions))
        reactions.sort()

        rows = []
        for reaction_id in reactions:
            rates = [reaction_id]
            for model_num, model in enumerate(self.models):
                found_kinetic_law = False
                r = model.select_one("listOfReactions").find(id=reaction_id)
                if r:
                    kinetic_law = r.select_one("kineticLaw")
                    if kinetic_law:
                        math_tag = kinetic_law.select_one("math")
                        rates.append(convert_rate_law(math_tag))
                        found_kinetic_law = True

                if not found_kinetic_law:
                    rates.append("-")

                r = rates[1:]
                if r.count(r[0]) != len(r):
                    self.generate_dot.differences_found = True

            rows.append(rates)

        print tabulate(rows, ["Reaction"] + self.model_names, tablefmt=output_format)

    def compare_params(self, output_format="simple"):
        """
        Print a table of parameter values, in which rows correspond to reactions and columns to models.

        Parameters
        ----------
        output_format : a table format supported by tabulate (e.g. simple, html)
        """

        models = map(lambda x: BeautifulSoup(x, 'xml'), self.model_strings)

        param_value = {}
        for model_num, model in enumerate(models):
            param_ids, param_values = get_params(model)

            for param_id in param_ids:

                if param_id not in param_value.keys():
                    param_value[param_id] = {}
                param_value[param_id][model_num] = param_values[param_id]

        rows = []
        for param_id in param_value.keys():
            row = [param_id]
            for model_num, model in enumerate(models):
                if model_num in param_value[param_id].keys():
                    row.append(param_value[param_id][model_num])
                else:
                    row.append("-")

                p = row[1:]
                if p.count(p[0]) != len(p):
                    self.generate_dot.differences_found = True
            rows.append(row)

        print tabulate(rows, ["Parameter"] + self.model_names, tablefmt=output_format)

    def diff_events(self):
        """
        Compare all events between models.
        The id attribute is optional for event elements. For simplicity, we ignore ids even if they are present, so that
        two non-identical events between models are treated as entirely separate; it would be nicer if color of only
        those visual elements corresponding to what actually changed.
        """

        event_status = {}
        event_objects = {}

        for model_num, model in enumerate(self.models):
            event_list = model.select_one("listOfEvents")
            if not event_list:
                continue

            for event in event_list.select('event'):

                if 'id' not in event.attrs.keys():
                    event.attrs["id"] = str(hash(event))
                event_id = event.attrs["id"]

                if event_id not in event_status.keys():
                    event_status[event_id] = []

                event_status[event_id].append(model_num)
                event_objects[event_id] = event

        for event_id in event_objects.keys():
            self.diff_event_with_id(event_id, event_status[event_id])

    def diff_event_with_id(self, event_id, model_set):

        diff_event = self.diff_object.add_event()

        # process trigger statement
        event_name = ""

        for model_num in model_set:
            species_ids = self.species_compartment[model_num].keys()
            event = self.models[model_num].select_one('#' + event_id)

            # process model name
            if not event_name and "name" in event.attrs.keys():
                event_name = event.attrs["name"]

            # process trigger statements
            trigger = event.select_one("trigger")
            if trigger:
                for ci in trigger.select("ci"):
                    entity = ci.text.strip()
                    if entity in species_ids:
                        diff_event.add_trigger_species(entity, event_id, model_num)
                    else:
                        diff_event.add_param(entity, event_id, model_num)

                trigger_expr = trigger.select_one("math")
                if not trigger_expr:
                    trigger_expr = ""
                trigger_expr = convert_rate_law(trigger_expr)
                diff_event.add_trigger(trigger_expr, model_num)

            event_assignments = event.select("eventAssignment")
            if event_assignments:
                for event in event_assignments:
                    if isinstance(event, NavigableString):
                        continue

                    # math
                    math = event.select_one("math")
                    if not math:
                        math = ""
                    converted_math = convert_rate_law(math)

                    # arrow to species set
                    variable_id = event.attrs["variable"]
                    if variable_id in species_ids:
                        diff_event.add_set_species(variable_id, converted_math, model_num)

                    elif self.show_params:
                        diff_event.add_set_species(variable_id, converted_math, model_num)

                    # arrow from species affecting expression
                    for ci in math.select("ci"):
                        species = ci.text.strip()
                        arrow_direction = categorise_interaction(math.parent, species, self.initial_value[model_num], use_sympy=self.use_sympy)

                        if species in species_ids:
                            diff_event.add_event_affect_value_arrow(variable_id, species, event_id, arrow_direction, model_num)
                        else:
                            diff_event.add_assignment_param_arrow(variable_id, species, event_id, arrow_direction, model_num)

        # record event node
        diff_event.set_event(event_id, event_name, model_set)

    def diff_algebraic_rules(self):
        """
        Compare all algebraic rules between models.
        """

        rule_diffs = {}

        for model_num, model in enumerate(self.models):

            rule_list = model.select_one("listOfRules")
            if not rule_list:
                continue

            for rule in rule_list.select("algebraicRule"):

                # find species occurring in this rule
                species_ids = []
                species_list = model.select_one("listOfSpecies")
                species_in_rule = []
                params_in_rule = []

                if species_list:
                    for s in species_list.select("species"):
                        species_ids.append(s.attrs["id"])

                for ci in rule.select("ci"):
                    species_id = ci.string.strip()
                    if species_id in species_ids:
                        species_in_rule.append(species_id)
                    else:
                        params_in_rule.append(species_id)

                # Choose an id  to represent this rule
                if "metaid" in rule.attrs.keys():
                    rule_id = rule.attrs["metaid"]
                else:
                    rule_id = "assignmentRule" + "_".join(species_in_rule)
                if rule_id not in rule_diffs.keys():
                    rule_diffs[rule_id] = self.diff_object.compartments["NONE"].add_rule(rule_id)

                for species_id in species_in_rule:
                    rule_diffs[rule_id].add_algebraic_arrow(model_num, rule_id, species_id)

                for param_id in params_in_rule:
                    rule_diffs[rule_id].add_parameter_rule(model_num, rule_id, param_id, 'none')

                rate_law = rule.select_one("math")
                if not rate_law:
                    rate_law = ""
                converted_rate_law = convert_rate_law(rate_law)
                rule_diffs[rule_id].add_rate_law(model_num, converted_rate_law)

    def diff_rules(self):
        """
        Compare all (rate or assignment) rules between models.
        """
        rule_targets = set()
        for model_num, model in enumerate(self.models):
            these_rule_targets = get_variables_set_by_rules(model)

            for rule_target in these_rule_targets:
                species_list = model.select_one('listOfSpecies')
                if not species_list or rule_target not in self.species_compartment[model_num].keys():
                    if rule_target not in self.modified_params.keys():
                        self.modified_params[rule_target] = set()
                    self.modified_params[rule_target].add(model_num)

                rule_targets.add(rule_target)

        for rule_target in rule_targets:
            self.diff_rule(rule_target)

    def diff_rule(self, target_id):
        """
        Compare a single rule between models.

        Parameters
        ----------
        target_id : id of the species affected by this rule
        """
        # if a reaction is shared, we need to consider whether its products, reactants and rate law are also shared

        # 'modifiers' appear in the math expression of a rule that sets 'target'
        # a rule has only one target, whereas reaction may have multiple products

        # Rules assigned to different compartments are considered to be distinct, event if they have the same targer

        diff_rules = {}
        for model_num, model in enumerate(self.models):
            _, compartment, rate_law = get_rule_details(model, target_id, self.species_compartment[model_num])

            self.diff_object.check_compartment_exists(compartment)
            if compartment not in diff_rules.keys():
                diff_rules[compartment] = self.diff_object.compartments[compartment].add_rule(target_id)

            if not rate_law:
                rate_law = ""

            converted_rate_law = convert_rate_law(rate_law)
            diff_rules[compartment].add_rate_law(model_num, converted_rate_law)

            entities = rate_law.select("ci")
            for entity in entities:
                entity = entity.string.strip()
                arrow_direction = categorise_interaction(rate_law.parent, entity, self.initial_value[model_num], use_sympy=self.use_sympy)

                if entity in self.species_compartment[model_num].keys():
                    diff_rules[compartment].add_modifier_arrow(model_num, target_id, entity, arrow_direction)
                else:
                    diff_rules[compartment].add_parameter_rule(model_num, target_id, entity, arrow_direction)

            # targets
            if self.show_params or (target_id in self.species_compartment[model_num].keys()):
                diff_rules[compartment].add_target_arrow(model_num, target_id)

    def diff_reactions(self):
        """
        Compare all reactions between models.
        """

        reaction_list = set()
        for model_num, model in enumerate(self.models):
            reactions = get_reactions(model)
            for reaction in reactions:
                reaction_list.add(reaction)

        for reaction_id in reaction_list:
            self.diff_reaction(reaction_id)

    def diff_reaction(self, reaction_id):
        """
        Compare a single reaction between models.

        Parameters
        ----------
        reaction_id : id of the reaction
        """

        # We need to consider whether the reaction's products, reactants and rate law are shared
        product_stoichiometries = {}
        is_transcription = False

        for model_num, model in enumerate(self.models):
            if reaction_id not in self.reactions[model_num].keys():
                continue
            reaction = self.reactions[model_num][reaction_id]

            reactants, products, compartment, rate_law, rs, ps = get_reaction_details(model, reaction, self.species_compartment[model_num])

            # Skip processing reaction if it should not be drawn for this model
            show_reaction = True
            if self.cartoon:
                if reaction in self.elided_reactions[model_num]:
                    show_reaction = False

                # TODO: FIXME
                # If a reaction has only one reaction, and it is an elided species (e.g. translation, mRNA degredation), do not print anything
                if len(reactants) == 1 and reactants[0] in self.elided_list[model_num]:
                    show_reaction = False
                # TODO: what if only a modifier

                # Hide all degredaion
                if len(reactants) == 1 and len(products) == 0:
                    show_reaction = False

            if not show_reaction:
                continue

            if self.cartoon and "sboTerm" in reaction.attrs.keys() and \
                    reaction.attrs['sboTerm'] in ["SBO:0000183", "SBO:0000589"]:
                is_transcription = True

            # only perform comparison between models in which this reaction actually occurs
            if not reactants and not products and not compartment and not rate_law and not rs and not ps:
                continue

            is_fast = False
            if "fast" in reaction.attrs.keys() and reaction.attrs["fast"] in ['1', 'true']:
                is_fast = True
            is_irreversible = False
            if "reversible" in reaction.attrs.keys() and reaction.attrs["reversible"] in ['0', 'false']:
                is_irreversible = True

            converted_rate_law = convert_rate_law(rate_law)
            reaction_name = self.reaction_name[model_num][reaction_id]

            self.diff_object.check_compartment_exists(compartment)
            diff_compartment = self.diff_object.compartments[compartment]
            diff_reaction = diff_compartment.add_reaction(reaction_id, rate_law, reaction_name,
                                                          converted_rate_law, is_fast, is_irreversible,
                                                          is_transcription, model_num)

            # reactant arrows
            for ind, stoich in enumerate(rs):
                diff_reaction.add_reactant_arrow(reaction_id, reactants[ind], stoich, model_num)

            # product arrows
            for ind, stoich in enumerate(ps):

                # if producing something that's been elided, adjust arrows to point ot downstream species
                product = products[ind]
                if self.cartoon and product in self.elided_list[model_num]:
                    product = self.downstream_species[model_num][product]

                if is_transcription:
                    diff_reaction.add_transcription_product_arrow(reaction_id, product, stoich, model_num)
                else:
                    diff_reaction.add_product_arrow(reaction_id, product, stoich, model_num)

            # parameter arrows
            if rate_law:
                entities = rate_law.select("ci")
                for entity in entities:
                    param = entity.string.strip()

                    # check a param rather than species
                    if param in self.species_compartment[model_num].keys():
                        continue

                    arrow_direction = categorise_interaction(rate_law.parent, param, self.initial_value[model_num], use_sympy=self.use_sympy)
                    diff_reaction.add_parameter_arrow(reaction_id, param, arrow_direction, model_num)

    def find_downstream_species(self):
        """
        Identifies reactions which should be elided in cartoon mode. A reaction should be elided if:
        - it has sboTerm SBO:0000184 (translation)
        - it has exactly one modifier or reactant species, and this species does not feature as a reactant or modifier
        species of any reaction that is not translation or degredation
        - it has exactly one product
        - it does not appear in more than one model, unless it has the same kineticLaw in each (as eliding the reaction
        would hide this difference)
        """

        for model_num, model in enumerate(self.models):
            # Only elide reactions with sboTerm corresponding to translation, and only one reactant/modifier species

            self.elided_list.append([])
            self.elided_reactions.append([])
            self.downstream_species.append({})

            # first, form a list of species that cannot safely be elided, because they are a reactant or modifier in a
            # reaction other than degredation or translation
            non_intermediates = []
            for reaction in model.select('reaction'):

                # skip degredation or translation reactions
                if "sboTerm" in reaction.attrs.keys() and reaction.attrs["sboTerm"] in ["SBO:0000184", "SBO:0000179"]:
                    continue

                reactant_list = reaction.select_one("listOfReactants")
                if reactant_list:
                    for reactant in reactant_list.select("speciesReference"):
                        non_intermediates.append(reactant.attrs["species"])

                modifier_list = reaction.select_one("listOfModifiers")
                if modifier_list:
                    for r in modifier_list.select("modifierSpeciesReference"):
                        non_intermediates.append(r["species"])

            # Now loop through reactions, identifying those that should be elided
            for reaction in model.select('reaction'):

                if "sboTerm" not in reaction.attrs.keys() or reaction.attrs["sboTerm"] != "SBO:0000184":
                    continue

                # if reaction has different kineticLaw in different models, don't elide it
                rate_laws = ""
                for m in self.models:
                    r = m.select_one("listOfReactions").find(id=reaction["id"])
                    if not r:
                        continue

                    rate_law = r.select_one("kineticLaw").select_one("math")
                    if rate_law and not rate_laws:
                        rate_laws = rate_law
                    elif rate_laws and rate_law and rate_laws != rate_law:
                        rate_laws = "different"
                        break

                if rate_laws == "different":
                    continue

                reactants_and_modifier_species = []

                # Check exactly one modifier/reactant
                modifier_list = reaction.select_one("listOfModifiers")
                if modifier_list:
                    for r in modifier_list.select("modifierSpeciesReference"):
                        reactants_and_modifier_species.append(r["species"])

                reactant_list = reaction.select_one("listOfReactants")
                if reactant_list:
                    for r in reactant_list.select("speciesReference"):
                        reactants_and_modifier_species.append(r["species"])

                if len(reactants_and_modifier_species) != 1:
                    continue

                species_to_elide = reactants_and_modifier_species[0]

                if species_to_elide in non_intermediates:
                    continue

                # check exactly one product (other than reactant, in case reaction is modelled as mRNA -> mRNA + protein)
                product_species = []
                product_list = reaction.select_one("listOfProducts")
                if product_list:
                    for p in product_list.select("speciesReference"):
                        product_id = p["species"]
                        if product_id != species_to_elide:
                            product_species.append(product_id)

                if len(product_species) != 1:
                    continue

                self.elided_list[model_num].append(species_to_elide)
                self.elided_reactions[model_num].append(reaction)
                self.downstream_species[model_num][species_to_elide] = product_species[0]

    def diff_compartment(self, compartment_id):
        """
        Print DOT output comparing a single compartment between models

        Parameters
        ----------
        compartment_id : the id of a compartment
        """

        diff_compartment = self.diff_object.check_compartment_exists(compartment_id)

        # Process all species
        for model_num, model in enumerate(self.models):
            for species in get_species(model, compartment_id):

                s = model.select_one("listOfSpecies").find(id=species)
                is_boundary = ""
                if "boundaryCondition" in s.attrs.keys():
                    is_boundary = s.attrs["boundaryCondition"]

                species_name = get_species_name(model, species)

                elided = False
                if self.cartoon and species in self.elided_list[model_num]:
                    elided = True

                diff_compartment.add_species(species, is_boundary, species_name, elided, model_num)

        # Process regulatory interactions
        for model_num, model in enumerate(self.models):
            if self.cartoon:
                arrows = get_regulatory_arrow(model, compartment_id, self.reactions[model_num], self.species_compartment[model_num], self.initial_value[model_num], elided_reactions=self.elided_reactions[model_num], use_sympy=self.use_sympy)
            else:
                arrows = get_regulatory_arrow(model, compartment_id, self.reactions[model_num], self.species_compartment[model_num], self.initial_value[model_num], use_sympy=self.use_sympy)

            for arrow in arrows:
                diff_compartment.add_regulatory_arrow(arrow[0], arrow[1], arrow[2], model_num)

    def diff_models(self):
        """
        Print DOT output comparing SBML models
        """

        self.check_model_supported()
        self.models = map(lambda x: inline_all_functions(x), self.models)

        if self.align:
            align_models(self.models)

        self.diff_reactions()

        if not self.hide_rules:
            self.diff_rules()
            self.diff_algebraic_rules()

        compartment_ids = set()
        for model_num, model in enumerate(self.models):
            for compartment in model.select('compartment'):
                if "id" in compartment.attrs.keys():
                    compartment_ids.add(compartment.attrs["id"])

        self.diff_object.check_compartment_exists("NONE") # Is this necessary?
        for compartment_id in compartment_ids:
            self.diff_compartment(compartment_id)

        self.diff_events()
        if self.show_params:
            self.draw_modified_params()

        # actually print the results of comparison
        self.generate_dot.generate_dot(self.diff_object)

    def abstract_model(self, model, model_num):
        """
        For each pair of species in a model, determine if there is an interaction, and if so classify it.
        If a species is a reactant, it is not considered to interact with itself through that reaction, since any species
        increases the rate of its own degredation

        Parameters
        ----------
        model : bs4.BeautifulSoup object produced by parsing an SBML model

        model_num : index of the model being abstracted

        Returns
        -------
        interactions : a 2D list, in which entries interactions[modifier][product] indicate the effect of the species with
            id modifier on the species with id product - one of "increase-degredation", "decrease-degredation",
            "increase-production", or "decrease-production"

        species : id of each species in the model
        """

        # Get list of species
        species = set()
        for compartment in model.select('compartment'):
            compartment_id = compartment.attrs["id"]
            species = species.union(get_species(model, compartment_id))

        interactions = {}
        for modifier in species:
            interactions[modifier] = {}
            for target in species:
                interactions[modifier][target] = set()

        reactions = get_reactions(model)
        for reaction_id in reactions:
            reaction = self.reactions[model_num][reaction_id]
            reactant_list, product_list, compartment, rate_law, _, _ = get_reaction_details(model, reaction, self.species_compartment[model_num])

            # Identify all species that appear in kineticLaw
            modifiers = []
            for ci in rate_law.findAll("ci"):
                name = ci.text.strip()
                if name in species:
                    modifiers.append(name)
            modifiers = set(modifiers)

            for modifier in modifiers:
                for reactant in reactant_list:

                    # Any species increases the rate of its own degredation, so ignore this
                    if reactant == modifier:
                        continue

                    effect = categorise_interaction(rate_law.parent, modifier, self.initial_value[model_num], use_sympy=self.use_sympy)
                    if effect == "monotonic_increasing":
                        interactions[modifier][reactant].add("increase-degredation")
                    elif effect == "monotonic_decreasing":
                        interactions[modifier][reactant].add("decrease-degredation")

                for product in product_list:
                    effect = categorise_interaction(rate_law.parent, modifier, self.initial_value[model_num], use_sympy=self.use_sympy)
                    if effect == "monotonic_increasing":
                        interactions[modifier][product].add("increase-production")
                    elif effect == "monotonic_decreasing":
                        interactions[modifier][product].add("decrease-production")

        return interactions, species

    # TODO: compartments!
    def diff_abstract_models(self, ignored_species, elided_species):
        """
        Print DOT output comparing SBML models after abstraction (by abstract_model(model))

        Parameters
        ----------
        ignored_species : list of species to be simply removed
        elided_species : list of species to be removed, with interactions targeting them appropriately moved downstream

        """
        if not ignored_species:
            ignored_species = []
        if not elided_species:
            elided_species = []

        if self.align:
            align_models(self.models)

        effect_types = ["increase-degredation", "decrease-degredation", "increase-production", "decrease-production"]

        # Construct abstracted version of each model
        abstracted_model = [] * len(self.models)
        species_list = set()
        models_containing_species = {}
        is_boundary_species = {}

        for model_num, model in enumerate(self.models):
            abstract, species = self.abstract_model(model, model_num)

            abstracted_model.append(abstract)
            species_list = species_list.union(species)

            for s in species:
                if s not in models_containing_species.keys():
                    models_containing_species[s] = set()
                models_containing_species[s].add(model_num)

                species_object = model.select_one("listOfSpecies").find(id=s)
                is_boundary = ""
                if "boundaryCondition" in species_object.attrs.keys():
                    is_boundary = species_object.attrs["boundaryCondition"]

                if s not in is_boundary_species.keys():
                    is_boundary_species[s] = is_boundary
                elif is_boundary_species[s] != is_boundary:
                    is_boundary_species[s] = '?'

        species_list = species_list.difference(ignored_species)
        retained_species = species_list.difference(elided_species)

        self.generate_dot.print_header()

        for s in retained_species:
            model_num = list(models_containing_species[s])[0]
            species_name = get_species_name(self.models[model_num], s)
            self.generate_dot.print_species_node(models_containing_species[s], is_boundary_species[s], s, species_name)

        # Construct interactions[modifier][species][type] = set of model_numbers
        interactions = {}
        for s1 in species_list:
            interactions[s1] = {}
            for s2 in species_list:
                interactions[s1][s2] = {}
                for effect in effect_types:
                    interactions[s1][s2][effect] = set()

        for model_num, model in enumerate(self.models):
            for modifier in species_list:
                if model_num not in models_containing_species[modifier]:
                    continue

                for species in species_list:
                    if model_num not in models_containing_species[species]:
                        continue

                    effects = abstracted_model[model_num][modifier][species]
                    for effect_type in effects:
                        interactions[modifier][species][effect_type].add(model_num)

        if elided_species:
            interactions = self.elide(species_list, effect_types, interactions, elided_species)

        for modifier in retained_species:
            for species in retained_species:
                for effect_type in effect_types:
                    model_list = interactions[modifier][species][effect_type]
                    self.generate_dot.print_abstracted_arrow(model_list, modifier, species, effect_type)

        self.generate_dot.print_footer()

    def elide(self, species_list, effect_types, interactions, elided_species):
        """
        Removes certain species from a model, transfering interactions that target them onto the species that they produce.
        Intended for case of an intermediate that causes production of a downstream species (e.g. mRNA, which causes
        production of a protein)

        Parameters
        ----------
        interactions : interactions[modifier][target][effect_type] is set of model numbers for which species with id
                        modifier has effect effect_type on species with id target
        elided_species : list containing the ide of each species to elide
        species_list : list containing id of every species
        effect_types : list of the possible effect types

        Returns
        -------
         a modified interactions structure
        """
        elided_species = set(elided_species).intersection(species_list)
        for model_num, model in enumerate(self.models):

            # For each elided species,
            for s in elided_species:

                # find the 'downstream' species (eg. the protein produced from mRNA)
                downstream = False
                for s2 in species_list:
                    if model_num in interactions[s][s2]["increase-production"]:
                        downstream = s2

                if not downstream:
                    continue

                # Transfer interactions targeting the elided species to the downstream species
                for regulator in species_list:
                    for effect_type in effect_types:
                        if model_num in interactions[regulator][s][effect_type]:
                            interactions[regulator][downstream][effect_type].add(model_num)

        # Then remove the elided species
        for s in elided_species:
            interactions.pop(s)

            for s2 in species_list:
                if s2 in interactions.keys():
                    interactions[s2].pop(s)

        return interactions

    def draw_modified_params(self):
        for param_id in self.modified_params.keys():
            model_set = list(self.modified_params[param_id])
            name = param_id
            param = self.models[model_set[0]].find(id=param_id)
            if "name" in param.attrs.keys():
                name = param.attrs["name"]
            self.diff_object.add_param_node(param_id, name, model_set)
